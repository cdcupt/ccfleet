"""Slot-first setup stays complete and scoped to the holder."""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from ccfleetd import devin_ui, gateway_providers, sessions, slots, usersite
from ccfleetd.api import Context, build_server
from ccfleetd.bilingual import project_html
from ccfleetd.config import Config
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import Store, slot_login_key
from tests.test_cli_access import public_key
from tests.test_gateway_provider_controls import configured, native_report
from tests.test_html_document_preservation import VOID
from tests.test_usersite import Browser

NOW = 1700000000.0
SESSION = "synthetic-setup-session"


@dataclass(eq=False, repr=False)
class Element:
    tag: str
    attrs: dict[str, str]
    parents: tuple[Element, ...] = ()
    content: list = field(default_factory=list)

    @property
    def text(self):
        return "".join(item.text if isinstance(item, Element) else item for item in self.content)

    def has_class(self, value):
        return value in self.attrs.get("class", "").split()


class Flow(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.elements, self.stack = [], []
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attributes):
        element = Element(tag, dict(attributes), tuple(self.stack))
        self.elements.append(element)
        if self.stack:
            self.stack[-1].content.append(element)
        if tag not in VOID:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, value):
        if self.stack:
            self.stack[-1].content.append(value)

    def find(self, tag=None, **attributes):
        return [element for element in self.elements
                if (tag is None or element.tag == tag)
                and all(element.attrs.get(name.replace("_", "-")) == value
                        for name, value in attributes.items())]

    def inside(self, outer, tag=None):
        return [element for element in self.elements if outer in element.parents
                and (tag is None or element.tag == tag)]


@pytest.fixture
def setup_context():
    store = Store(":memory:")
    account = store.add_account("alice", "sub-alice", "alice@example.invalid", slot_quota=2,
                                now=NOW)
    store.add_account("bob", "sub-bob", "bob@example.invalid", slot_quota=1, now=NOW)
    cfg = configured(Config(cookie_secret="synthetic-cookie-secret-0123456789"))
    for number, (identifier, owner, name, signed_in) in enumerate((
        ("claude", "alice", "alice-claude", True),
        ("devin", "alice", "alice-devin", False),
        ("foreign", "bob", "bob-private", True),
    ), 1):
        node = "machine-" + str(number)
        store.add_node(node, "operator", now=NOW)
        store.set_node_access(node, "192.0.2." + str(number), 2222, public_key(number))
        store.add_slot(identifier, node, "slot01", now=NOW)
        store.apply_slot_report(node, [{"unix_user": "slot01", "present": False}], now=NOW)
        assert store.claim_slot(owner, now=NOW)["id"] == identifier
        store.move_slot(identifier, slots.CLAIMED)
        if signed_in:
            store.move_slot(identifier, slots.ACTIVE)
        store.name_slot(identifier, name, held_by=owner)
        store.insert_heartbeat(node, NOW, {"node_id": node, "mode": "machine", "slots": [
            {"unix_user": "slot01", "credentials": {"logged_in": signed_in}}]})
    store._conn.execute("UPDATE slots SET present=1")
    store._conn.commit()
    target = store.get_slot("devin")
    binding = store.bind_gateway_provider("devin", "alice", "devin", target["gateway_claim_id"],
        native_report(NOW), expected_generation=0, now=NOW)
    existing = store.create_gateway_key("alice", "Existing Devin connection", ["devin"],
        provider="devin", allow_devin=True,
        provider_runtime=gateway_providers.runtime_expectations(cfg), now=NOW)
    foreign = store.create_gateway_key("bob", "Bob private key", ["foreign"], now=NOW)
    yield SimpleNamespace(store=store, cfg=cfg, account=account, binding=binding,
                          existing=existing, foreign=foreign)
    store.close()


def rendered(c, language="en", now=NOW):
    raw = usersite.page(c.store, c.cfg, c.account, SESSION, now)
    return project_html(raw, language, target="/" + language + "/account")


def connection(parsed, provider="devin"):
    [result] = parsed.find("details", data_slot_connect=provider)
    return result


@pytest.mark.parametrize("language", ["en", "zh"])
def test_setup_precedes_launch_with_distinct_own_authorizer_and_Devin_target(setup_context, language):
    c = setup_context
    source = rendered(c, language)
    assert 'http-equiv="refresh"' not in source
    assert ('Refresh status' if language == 'en' else '刷新状态') in source
    page = Flow(source)
    commands = [element.text for element in page.inside(connection(page), "code")]
    assert commands == [
        "ccfleet update",
        "ccfleet gateway setup --slot alice-claude --slots alice-devin --provider devin",
        "ccfleet local --transport gateway --slot alice-devin --model claude-opus-5-5 "
        "--effort max --new -- --safe-mode",
    ]
    assert "bob-private" not in source and "Bob private key" not in source
    secrets = (c.existing["key_token"], c.foreign["key_token"], SESSION,
               c.binding["account_fp"], c.binding["claim_id"],
               usersite.csrf_for(SESSION, c.cfg.cookie_secret))
    assert all(secret not in command for secret in secrets for command in commands)
    assert c.existing["key_token"] not in source and c.foreign["key_token"] not in source


@pytest.mark.parametrize("language", ["en", "zh"])
def test_first_computer_form_is_POST_with_session_CSRF_and_own_source_only(setup_context, language):
    c = setup_context
    page = Flow(rendered(c, language))
    [form] = page.inside(connection(page), "form")
    assert form.attrs["method"] == "post"
    assert form.attrs["action"] == ("/zh" if language == "zh" else "") + "/account/slots/claude/cli"
    [token] = page.inside(form, "input")
    assert token.attrs == {"type": "hidden", "name": "csrf",
                           "value": usersite.csrf_for(SESSION, c.cfg.cookie_secret)}
    assert not page.find("form", action="/account/slots/devin/cli")
    assert not page.find("form", action="/account/slots/foreign/cli")


def test_pairing_endpoint_rejects_missing_CSRF_and_foreign_source(setup_context):
    c = setup_context
    cfg = replace(c.cfg, cookie_secure=False)
    server = build_server(Context(c.store, cfg, Monitor(c.store, cfg, LogNotifier())),
                          host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = c.store.create_session("alice", now=time.time(), ttl_s=3600)
    browser = Browser(server.server_address[1])
    browser.jar[sessions.COOKIE_NAME] = sessions.sign(session, cfg.cookie_secret)
    headers = {"Host": "fleet.example.com"}
    csrf = usersite.csrf_for(session, cfg.cookie_secret)
    try:
        before = c.store._conn.execute("SELECT COUNT(*) FROM cli_pairings").fetchone()[0]
        assert browser.call("POST", "/account/slots/claude/cli", form={}, headers=headers).status == 403
        assert browser.call("POST", "/account/slots/foreign/cli", form={"csrf": csrf},
                            headers=headers).status == 404
        assert c.store._conn.execute("SELECT COUNT(*) FROM cli_pairings").fetchone()[0] == before
        assert browser.call("POST", "/account/slots/claude/cli", form={"csrf": csrf},
                            headers=headers).status == 200
        rows = c.store._conn.execute("SELECT slot_id FROM cli_pairings").fetchall()
        assert [row[0] for row in rows] == ["claude"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("language", ["en", "zh"])
def test_Devin_only_account_explains_manual_fallback_without_fake_pairing(setup_context, language):
    c = setup_context
    c.store.begin_release("claude", held_by="alice")
    page = Flow(rendered(c, language))
    guide = connection(page)
    assert not page.inside(guide, "form") and not page.inside(guide, "code")
    assert "ccfleet gateway setup" not in guide.text and "ccfleet local" not in guide.text
    assert any(element.attrs.get("href", "").endswith("/account#gateway")
               for element in page.inside(guide, "a"))
    assert ("assisted Fleet CLI setup" if language == "en" else "需要协助设置 Fleet CLI") in guide.text
    assert ("create a connection key" if language == "en" else "创建密钥") in guide.text


@pytest.mark.parametrize("fault", ["no-binding", "identity-failed", "expired", "disabled"])
def test_unready_Devin_does_not_advertise_ready_launch_commands(setup_context, fault):
    c, now = setup_context, NOW
    if fault == "no-binding":
        c.store._conn.execute("DELETE FROM gateway_provider_bindings WHERE slot_id='devin'")
        c.store._conn.commit()
    elif fault == "identity-failed":
        c.store.bind_gateway_provider("devin", "alice", "devin", c.binding["claim_id"],
            native_report(NOW, identity_verified=False, account_fp=None, capabilities={}),
            expected_generation=c.binding["generation"], now=NOW)
    elif fault == "expired":
        now += 301
    else:
        c.cfg = replace(c.cfg, gateway_devin_enabled=False)
    assert devin_ui.native_state(c.store, c.cfg, c.account, c.store.get_slot("devin"), now=now) != "ready"
    page = Flow(rendered(c, now=now))
    assert not page.find("details", data_slot_connect="devin")
    assert "ccfleet local --transport gateway --slot alice-devin" not in rendered(c, now=now)


@pytest.mark.parametrize("stage", [None, "requested", "url_ready"])
def test_Claude_first_sign_in_and_pending_authorization_stay_expanded(setup_context, stage):
    c = setup_context
    c.store._conn.execute("UPDATE slots SET state=? WHERE id='claude'", (slots.CLAIMED,))
    c.store._conn.commit()
    c.store.insert_heartbeat("machine-1", NOW, {"node_id": "machine-1", "mode": "machine",
        "slots": [{"unix_user": "slot01", "credentials": {"logged_in": False}}]})
    if stage:
        c.store.request_slot_login("claude", "", NOW, held_by="alice")
    if stage == "url_ready":
        c.store.record_login_progress(slot_login_key("claude"), "url_ready",
            "https://claude.com/cai/oauth/authorize?client_id=synthetic&state=synthetic", "", NOW,
            requested_at=NOW)
    page = Flow(rendered(c))
    [card] = page.find("div", id="slot-claude")
    [account] = [element for element in page.inside(card, "details") if element.has_class("slot-account")]
    assert "open" in account.attrs
    assert all("open" in ancestor.attrs for ancestor in account.parents if ancestor.tag == "details")
    if stage == "url_ready":
        assert page.find("form", action="/account/slots/claude/code")
        assert page.find("a", href="https://claude.com/cai/oauth/authorize?client_id=synthetic&state=synthetic")
    elif stage is None:
        assert page.find("form", action="/account/slots/claude/signin")
    else:
        assert page.find("form", action="/account/slots/claude/cancel")
        assert any(element.has_class("login-say") for element in page.inside(account, "span"))


@pytest.mark.parametrize("language", ["en", "zh"])
def test_controls_are_single_language_and_advanced_connections_follow_cards(setup_context, language):
    source = rendered(setup_context, language)
    page = Flow(source)
    labels = [element.text for element in page.inside(connection(page))
              if element.tag in {"summary", "button", "b"}]
    expected = ("Save this connection", "Start coding", "Connect this computer")
    translated = ("保存这个连接", "开始写代码", "连接电脑")
    own, other = (expected, translated) if language == "en" else (translated, expected)
    assert all(any(text in label for label in labels) for text in own)
    assert all(not any(text in label for label in labels) for text in other)
    if language == "en":
        assert all(not re.search(r"[\u4e00-\u9fff]", label) for label in labels)
    assert source.index('id="slot-devin"') < source.index('id="gateway"')
    [advanced] = page.find("details", id="gateway")
    assert "open" not in advanced.attrs


def test_CSS_has_flexible_desktop_grid_mobile_stack_and_wrapping_commands(setup_context):
    page = Flow(rendered(setup_context))
    css = "\n".join(element.text for element in page.find("style"))
    assert re.search(r"\.account-dashboard \.slots\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)", css)
    assert re.search(r"@media\s*\(max-width:\s*\d+px\)\s*\{\s*\.account-dashboard \.slots\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)", css)
    assert re.search(r"\.account-dashboard pre\s*\{[^}]*max-width:\s*100%[^}]*white-space:\s*pre-wrap[^}]*overflow-wrap:\s*anywhere", css)
    assert re.search(r"\.account-dashboard \.connect-steps>li>div\s*\{[^}]*min-width:\s*0", css)
