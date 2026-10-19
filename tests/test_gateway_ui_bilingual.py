"""Bilingual connection controls retain their ownership and secret boundaries."""
from __future__ import annotations

from dataclasses import replace
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from ccfleetd import gateway_ui, slots
from ccfleetd.config import Config
from ccfleetd.store import Store

NOW = 1700000000.0


class Controls(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.elements = []
        self.details = []
        self.open_details = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        if tag == "details":
            section = {"attrs": attrs, "parents": list(self.open_details)}
            self.details.append(section)
            self.open_details.append(attrs)
        if tag == "form":
            assert self.current is None, "forms must not be nested"
            self.current = {"attrs": attrs, "inputs": []}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None:
            self.current["inputs"].append(attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None
        elif tag == "details":
            self.open_details.pop()


@pytest.fixture
def context(monkeypatch):
    store = Store(":memory:", max_slots_per_machine=8)
    store.add_node("machine", "operator", now=NOW)
    store.set_machine_capacity("machine", 3)
    account = store.add_account("alice", "google-alice", "alice@example.com", slot_quota=2,
                                now=NOW)
    other = store.add_account("bob", "google-bob", "bob@example.com", slot_quota=1, now=NOW)
    for number in range(1, 4):
        store.add_slot(f"slot-{number}", "machine", f"slot0{number}", now=NOW)
    store.apply_slot_report("machine", [{"unix_user": f"slot0{number}", "present": False}
                                        for number in range(1, 4)], now=NOW)
    one = store.claim_slot(account["id"], now=NOW)
    two = store.claim_slot(account["id"], now=NOW)
    third = store.claim_slot(other["id"], now=NOW)
    monkeypatch.setattr(gateway_ui, "available", lambda cfg: True)
    cfg = Config(public_url="https://fleet.invalid/", cookie_secret="x" * 32)
    yield SimpleNamespace(store=store, account=account, other=other, one=one, two=two,
                          third=third, cfg=cfg)
    store.close()


def render(c, csrf="csrf"):
    return gateway_ui.panel(c.store, c.cfg, c.account, csrf,
                            c.store.list_slots(held_by=c.account["id"]))


def test_bilingual_cards_keep_form_actions_and_owned_priority_values(context):
    c = context
    key = c.store.create_gateway_key("alice", "Laptop", [c.two["id"], c.one["id"]],
                                     mode="priority", now=NOW)
    private = c.store.create_gateway_key("bob", "Bob private", [c.third["id"]], now=NOW)
    html = render(c, 'csrf"<&')
    parsed = Controls(html)
    assert [form["attrs"]["action"] for form in parsed.forms] == [
        "/account/gateway/update", "/account/gateway/revoke", "/account/gateway/create"]
    for form in parsed.forms:
        assert form["attrs"]["method"] == "post"
        csrf = [field for field in form["inputs"] if field["name"] == "csrf"]
        assert len(csrf) == 1 and csrf[0]["value"] == 'csrf"<&'
    update = {field["name"]: field for field in parsed.forms[0]["inputs"]}
    assert update["slot." + c.two["id"]]["value"] == "1"
    assert update["slot." + c.one["id"]]["value"] == "2"
    assert update["key_id"]["value"] == key["id"]
    options = [attrs for tag, attrs in parsed.elements if tag == "option"]
    assert {option["value"] for option in options} == {"sticky", "priority"}
    assert all(option.get("data-copy-en") and option.get("data-copy-zh") for option in options)
    assert "selected" in options[1] and "selected" not in options[0]
    assert 'name="slot.' + c.third["id"] + '"' not in html
    assert "Bob private" not in html and private["key_token"] not in html
    assert key["key_token"] not in html and "token_hash" not in html
    assert 'lang="en"' in html and 'lang="zh-Hans"' in html
    assert "高级连接设置" in html and "Advanced connections" in html
    assert "API endpoints" not in html


@pytest.mark.parametrize("with_key", [False, True])
def test_advanced_connections_start_closed_and_keep_setup_secondary(context, with_key):
    c = context
    if with_key:
        c.store.create_gateway_key("alice", "Laptop", [c.one["id"]], now=NOW)
    html = render(c)
    parsed = Controls(html)
    outer = parsed.details[0]
    assert outer["attrs"]["id"] == "gateway"
    assert outer["attrs"]["aria-labelledby"] == "gateway-title"
    assert not outer["parents"]
    assert "account-advanced" in outer["attrs"]["class"].split()
    assert all("open" not in section["attrs"] for section in parsed.details)
    for section in parsed.details[1:]:
        assert section["parents"][0]["id"] == "gateway"
    assert "Use the steps on your slot card" in html
    assert "首次使用，请按槽位卡片上的步骤操作" in html
    assert "Saved connection keys" in html and "已保存的连接密钥" in html
    assert "ccfleet local</code></pre>" not in html


def test_untrusted_names_identifiers_and_address_are_escaped(context):
    c = context
    c.store.create_gateway_key("alice", '<svg onload="attack()">', [c.one["id"]], now=NOW)
    held = c.store.list_slots(held_by="alice")
    held[0] = dict(held[0], name='<img src=x onerror="attack()">')
    cfg = replace(c.cfg, public_url='https://fleet.invalid/<script>attack()</script>')
    html = gateway_ui.panel(c.store, cfg, c.account, 'csrf"<>', held)
    tags = [tag for tag, _ in Controls(html).elements]
    assert "svg" not in tags and "img" not in tags and "script" not in tags
    assert '&lt;svg onload=&quot;attack()&quot;&gt;' in html
    assert '&lt;img src=x onerror=&quot;attack()&quot;&gt;' in html
    assert '&lt;script&gt;attack()&lt;/script&gt;' in html
    unsafe_id = 'id" autofocus onfocus="attack()'
    choices = Controls(gateway_ui._choices([
        {"id": unsafe_id, "name": "Name", "kind": slots.MACHINE_SLOT, "state": slots.ACTIVE}
    ], [unsafe_id]))
    fields = [attrs for tag, attrs in choices.elements if tag == "input"]
    assert len(fields) == 1 and fields[0]["name"] == "slot." + unsafe_id
    assert "autofocus" not in fields[0] and "onfocus" not in fields[0]


def test_empty_state_does_not_offer_an_unusable_form(context):
    c = context
    html = gateway_ui.panel(c.store, c.cfg, c.account, "csrf", [])
    assert "Start with a slot" in html and "先准备一个槽位" in html
    assert not Controls(html).forms
    assert "No saved connections yet" in html and "还没有保存的连接" in html
    assert "ccfleet gateway setup" in html
    assert "does not import a key created on this page" in html
    assert "不会导入本页手动创建的密钥" in html


def test_choices_exclude_releasing_and_owner_slots(context):
    c = context
    held = [dict(c.one, state=slots.ACTIVE), dict(c.two, state=slots.RELEASING),
            dict(c.third, kind=slots.OWNER_SLOT, state=slots.ACTIVE)]
    html = gateway_ui._choices(held, [c.one["id"]])
    assert 'name="slot.' + c.one["id"] + '"' in html
    assert 'name="slot.' + c.two["id"] + '"' not in html
    assert 'name="slot.' + c.third["id"] + '"' not in html
    assert "只选一个槽位，就始终使用这个账号" in html


def test_creation_reveals_one_escaped_secret_outside_translated_copy(context, monkeypatch):
    c = context
    secret = 'ccf_api_test<script>alert("key")</script>'
    monkeypatch.setattr(c.store, "create_gateway_key", lambda *args, **kwargs:
                        {"key_token": secret})
    result = gateway_ui.act(c.store, c.cfg, c.account, "/account/gateway/create",
                            {"slot." + c.one["id"]: "1", "mode": "sticky", "name": "Laptop"},
                            NOW)
    assert result.status == 200
    assert result.body.count('ccf_api_test') == 1
    assert '<pre class="token">ccf_api_test&lt;script&gt;' in result.body
    assert secret not in result.body
    assert "only its hash" in result.body and "服务器只保存它的哈希值" in result.body
    assert "Save your connection key" in result.body and "保存你的连接密钥" in result.body
    assert "It does not import the website key shown above" in result.body
    assert "它不会导入上方的网页密钥" in result.body
    assert "ANTHROPIC_BASE_URL" in result.body and "ANTHROPIC_AUTH_TOKEN" in result.body
    assert 'href="/account#gateway"' in result.body


def test_routing_help_covers_safe_switching_and_privacy_in_both_languages(context):
    html = render(context)
    assert "briefly move behind other choices" in html and "暂时排到其他选择后面" in html
    assert "may be skipped briefly" not in html
    assert "up to 24 hours" in html and "最多保留 24 小时" in html
    assert "outcome is unknown" in html and "不知道请求是否已经送达" in html
    assert "after response headers have been sent" in html and "已向客户端发出响应头" in html
    assert "Applications may have their own retry behavior" in html
    assert "gateway can read model" in html and "网关在转发时能够读取模型请求和回复" in html
    assert "credential remains on its slot" in html
    assert "with no automatic switch to another account" in html
