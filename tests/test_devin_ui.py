"""Own-slot Devin guidance uses real trusted Store admission, with no login action."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from ccfleetd import devin_ui, gateway_providers, slots
from ccfleetd.config import Config
from ccfleetd.store import Store
from tests.test_cli_access import active_slot
from tests.test_gateway_provider_controls import configured, native_report
from tests.test_html_document_preservation import Document

NOW = 1700000000.0


@pytest.fixture
def context():
    store = Store(":memory:")
    active_slot(store)
    store._conn.execute("UPDATE slots SET present=1")
    store._conn.commit()
    cfg = configured(Config())
    yield SimpleNamespace(store=store, cfg=cfg, account={"id": "a1"},
                          slot=store.get_slot("s1"))
    store.close()


def bind(c, **changes):
    return c.store.bind_gateway_provider("s1", "a1", "devin", c.slot["gateway_claim_id"],
        native_report(NOW, **changes), expected_generation=0, now=NOW)


def render(c, *, now=NOW):
    return devin_ui.native_details(c.store, c.cfg, c.account, c.slot, now=now)


def test_ready_only_after_trusted_current_verification(context):
    c = context
    bind(c)
    html = render(c)
    document = Document(html)
    assert devin_ui.native_state(c.store, c.cfg, c.account, c.slot, now=NOW) == "ready"
    assert "Devin ready" in document.text and "Devin 已就绪" in document.text
    assert "/account#gateway" in document.links
    assert "You do not need to install Devin CLI locally" in document.text
    assert "无需在本机安装 Devin CLI" in document.text
    assert "Claude Code and its tools run on your computer" in document.text
    assert not any(tag in {"form", "input", "button", "script"} for tag, _ in document.tags)


def test_setup_is_guidance_not_a_fake_login_action(context):
    html = render(context)
    document = Document(html)
    assert 'data-native-state="setup_required"' in html
    assert "Setup required" in document.text and "需要设置" in document.text
    assert devin_ui.GUIDE in document.links
    assert "How to connect Devin" in document.text
    assert "setup is assisted" in document.text and "纯网页自助登录" in document.text
    assert "Signing in to Claude does not sign you in to Devin" in document.text
    assert "Log in to Devin" not in document.text
    assert "/account#gateway" not in document.links


@pytest.mark.parametrize("changes", [
    {"identity_verified": False, "account_fp": None, "capabilities": {}},
    {"capabilities": {name: False for name in gateway_providers.NATIVE_AGENT_CAPABILITIES}},
    {"adapter_revision": "f" * 64},
    {"native_version": "3000.11.93"},
])
def test_unverified_partial_or_wrong_runtime_never_ready(context, changes):
    bind(context, **changes)
    html = render(context)
    assert 'data-native-state="verification_required"' in html
    assert "Verification required" in Document(html).text
    assert "/account#gateway" not in Document(html).links


def test_stale_or_revoked_binding_requires_verification(context):
    c = context
    binding = bind(c)
    assert 'data-native-state="verification_required"' in render(c, now=NOW + 301)
    assert c.store.revoke_gateway_provider_binding("s1", "a1", "devin",
        c.slot["gateway_claim_id"], binding["generation"], now=NOW)
    assert 'data-native-state="verification_required"' in render(c)


def test_global_opt_out_never_shows_ready(context):
    c = context
    bind(c)
    c.cfg = replace(c.cfg, gateway_devin_enabled=False)
    assert 'data-native-state="ready"' not in render(c)
    assert "/account#gateway" not in Document(render(c)).links


def test_holder_report_and_claude_login_cannot_assert_devin_readiness(context):
    c = context
    c.slot.update(gateway_provider={"identity_verified": True, "account_fp": "a" * 64},
                  credentials={"logged_in": True}, claude={"logged_in": True})
    assert 'data-native-state="setup_required"' in render(c)


@pytest.mark.parametrize("change", [
    {"held_by": "another-person"}, {"kind": slots.OWNER_SLOT},
    {"state": slots.RELEASING}, {"state": slots.FREE}, {"id": ""},
])
def test_ineligible_or_foreign_slots_hidden_before_store_access(context, change):
    class NoRead:
        def __getattr__(self, name):
            pytest.fail("must not access another slot's provider state: " + name)
    c = context
    assert devin_ui.native_details(NoRead(), c.cfg, c.account,
                                  {**c.slot, **change}, now=NOW) == ""


def test_binding_and_secrets_are_not_rendered_and_no_mutation_occurs(context):
    c = context
    bind(c)
    c.store.create_gateway_key("a1", "Existing", ["s1"], now=NOW, provider="devin",
        allow_devin=True, provider_runtime=gateway_providers.runtime_expectations(c.cfg))
    before = list(c.store._conn.iterdump())
    html = render(c)
    assert list(c.store._conn.iterdump()) == before
    assert c.slot["gateway_claim_id"] not in html
    assert "a" * 64 not in html and "b" * 64 not in html and "c" * 64 not in html
    assert "token_hash" not in html and "credential_sha256" not in html
