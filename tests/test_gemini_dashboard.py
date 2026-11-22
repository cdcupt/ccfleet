"""Browser-to-Mac credential enrollment without sending a provider key to Fleet."""

import json
import re
from html.parser import HTMLParser

import pytest

from ccfleet_agent import mac_api_model as mac
from ccfleet_agent import mac_gemini_web as crypto
from ccfleetd import gemini_setup as ui
from ccfleetd.config import Config
from tests.test_private_owner_nodes import enable, endpoint

pytest_plugins = ("tests.test_private_owner_nodes",)


class Vault:
    def __init__(self):
        self.items = {}

    def get_password(self, service, name):
        return self.items.get((service, name))

    def set_password(self, service, name, value):
        self.items[service, name] = value

    def delete_password(self, service, name):
        del self.items[service, name]


class Form(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.data = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "form" and attr.get("id") == "gemini-credential":
            self.data = attr
        if tag == "input" and attr.get("name"):
            self.values[attr["name"]] = attr.get("value", "")


def encrypted(pub, nonce, secret):
    pytest.importorskip("cryptography")
    import os

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    header = crypto.b64(
        json.dumps(
            {"alg": "RSA-OAEP-256", "enc": "A256GCM", "kid": nonce}, separators=(",", ":")
        ).encode()
    )
    rsa = serialization.load_der_public_key(crypto.unb64(pub, 900))
    cek, iv = AESGCM.generate_key(bit_length=256), os.urandom(12)
    wrapped = rsa.encrypt(cek, padding.OAEP(padding.MGF1(hashes.SHA256()), hashes.SHA256(), None))
    raw = AESGCM(cek).encrypt(iv, secret.encode(), header.encode())
    return ".".join([header, *map(crypto.b64, (wrapped, iv, raw[:-16], raw[-16:]))])


def test_setup_encrypted_end_to_end_replay_and_disconnect(private):
    pytest.importorskip("cryptography")
    store, _ = private
    enable(store)
    key = endpoint(store)
    account = store.get_account("operator")
    cfg = Config(cookie_secret="s" * 32)
    vault = Vault()
    calls = []
    secret = "synthetic-provider-key-for-tests"

    def transport(slot, cfg, request):
        assert slot["id"] == "mac-private"
        assert secret not in json.dumps(request)
        calls.append(request)
        return crypto.action(mac, vault, request, 100)

    def post(action, fields, session="origin", now=100, owner=account):
        return ui.act(
            store,
            cfg,
            owner,
            session,
            action,
            {"csrf": "fixture", **fields},
            now,
            transport=transport,
        )

    started = post("start", {"key": key["id"], "project": "12345", "model": "gemini-test"})
    assert started.status == 200 and 'name="api_key"' not in started.body
    form = Form(started.body)
    envelope = encrypted(form.data["data-pub"], form.data["data-nonce"], secret)
    body = {"flow": form.values["flow"], "envelope": envelope}
    assert post("finish", body, session="different").status == 409
    assert post("finish", body, now=401).status == 409
    assert len(calls) == 1
    with pytest.raises(PermissionError):
        post("finish", body, owner=store.get_account("other-admin"))
    assert post("finish", body).status == 303
    assert post("finish", body).status != 303
    slot, _, binding, grants = store.gemini_setup_state("operator")
    assert grants == {key["id"]}
    targets = store.api_model_targets(key["id"], key["generation"])
    assert len(targets) == 1 and targets[0]["slot_id"] == slot["id"]
    assert secret not in str(targets) and secret not in str(binding)
    assert secret in vault.get_password(mac.SERVICE, binding["id"])
    assert post("start", {"key": key["id"]}).status == 303
    assert len(vault.items) == 1
    assert post("disconnect", {"binding": binding["id"], "confirm": "1"}).status == 303
    assert store.api_model_targets(key["id"], key["generation"]) == []
    assert not vault.items
    assert post("finish", body).status == 409


def test_disconnect_fails_closed_offline_and_can_finish(private):
    store, _ = private
    enable(store)
    acct = store.get_account("operator")
    key = endpoint(store)
    r = store.reserve_api_model(key["id"], "mac-private", "gemini", "12345", "gemini-test")
    store.grant_api_model(key["id"], "mac-private", {**r, "credential_fp": "a" * 64})
    args = (
        store,
        Config(cookie_secret="s" * 32),
        acct,
        "session",
        "disconnect",
        {"csrf": "yes", "confirm": "1", "binding": r["binding_id"]},
        100,
    )

    def offline(*args):
        raise ui.Unavailable()

    assert ui.act(*args, transport=offline).status == 503
    assert store.api_model_targets(key["id"], key["generation"]) == []
    with pytest.raises(ValueError):
        store.grant_api_model(key["id"], "mac-private", {**r, "credential_fp": "a" * 64})
    assert ui.act(*args, transport=lambda *_: {"removed": True}).status == 303


def test_stale_or_other_gateway_cannot_receive_secret(private):
    store, _ = private
    enable(store)
    k = endpoint(store)
    _, route, g, _ = store.gemini_setup_start("operator", k["id"], "12345", "gemini-test")
    flow = {"route": route, "key": k["id"], "generation": g}
    store.update_gateway_key(k["id"], "operator", ["mac-private"], "priority")
    with pytest.raises(ValueError):
        store.gemini_setup_continue("operator", flow)
    with pytest.raises(PermissionError):
        store.gemini_setup_start("customer", k["id"], "12345", "gemini-test")


def test_new_macbook_can_pair_without_a_previous_endpoint(private):
    from tests.test_gateway_access import public_key

    store, _ = private
    enable(store)
    # No prior laptop or service key. Explicit web setup admits the service for one heartbeat.
    assert store.gateway_public_keys("mac-private", public_key(3)) == []
    slot, route, gen, _ = store.gemini_setup_start("operator", "new", "12345", "gemini-test")
    assert store.gateway_public_keys(slot["id"], public_key(3)) == [public_key(3)]
    flow = {"key": "new", "route": route, "generation": gen}
    receipt = {**route, "credential_fp": "a" * 64}
    code = store.gemini_setup_continue("operator", flow, receipt, now=1000)
    paired = store.register_cli_device(code, public_key(7), "MacBook", now=1001)
    with pytest.raises(ValueError):
        store.register_cli_device(code, public_key(9), "Second", now=1002)
    key = store.create_gateway_key(
        "operator", "MacBook", [slot["id"]], now=1003, pairing_device_id=paired["device_id"]
    )
    (target,) = store.api_model_targets(key["id"], key["generation"])
    assert target["provider"] == "gemini" and target["slot_id"] == slot["id"]
    second_key = store.create_gateway_key(
        "operator",
        "Another endpoint",
        [slot["id"]],
        now=1003,
        pairing_device_id=paired["device_id"],
    )
    assert not store.api_model_targets(second_key["id"], second_key["generation"])
    plain = store.request_cli_pairing(slot["id"], "operator", now=1004)
    other = store.register_cli_device(plain, public_key(6), "Plain", now=1005)
    unrelated = store.create_gateway_key(
        "operator", "Plain", [slot["id"]], now=1006, pairing_device_id=other["device_id"]
    )
    assert not store.api_model_targets(unrelated["id"], unrelated["generation"])
    expiring = store.gemini_setup_continue("operator", flow, receipt, now=1007)
    with pytest.raises(ValueError):
        store.register_cli_device(expiring, public_key(8), "Expired", now=1608)
    fresh = store.gemini_setup_continue("operator", flow, receipt, now=1008)
    store.gemini_setup_disconnect("operator", route["binding_id"])
    with pytest.raises(ValueError):
        store.register_cli_device(fresh, public_key(8), "Disconnected", now=1009)


def test_disconnect_requires_exact_mac_binding():
    pytest.importorskip("cryptography")
    vault = Vault()
    r = {
        "provider": "openai",
        "model": "gpt-test",
        "binding_id": "a" * 32,
        "claim_id": "b" * 32,
        "account_fp": "c" * 64,
    }
    mac.save_profile(r, "synthetic-mac-credential", vault)
    with pytest.raises(ValueError, match="binding changed"):
        crypto.action(
            mac,
            vault,
            {"action": "forget", "nonce": "d" * 32, "reservation": {**r, "provider": "gemini"}},
            100,
        )
    assert vault.get_password(mac.SERVICE, r["binding_id"])


def test_client_picker_distinguishes_new_computers_from_saved_connection_names():
    from ccfleetd import bilingual, gemini_setup_ui

    keys = {"g123456": {"name": "This computer"}, "g345678": {"name": "This computer"}}
    form = gemini_setup_ui.setup_form("fixture", keys, None)
    for lang, new, saved in (("en", "Pair a new computer", "Saved connection"),
                             ("zh", "配对新电脑", "已有连接")):
        html = bilingual.project_html(form, lang, "/account/gemini")
        assert new in html and saved + " · This computer · 123456" in html
        assert 'for="gm-client"' in html and 'id="gm-client"' in html
        for name in ("gm-project", "gm-model"):
            assert f'for="{name}"' in html and f'aria-describedby="{name}-hint"' in html


def test_subscription_account_labels_follow_pairing_state(private):
    from ccfleetd import bilingual, usersite
    from tests.test_gateway_access import public_key
    from tests.test_private_owner_nodes import NOW

    store, _ = private
    enable(store)
    owner = store.get_account("operator")
    cfg = Config(cookie_secret="fixture", public_url="https://fleet.example",
                 gateway_enabled=True, gateway_ssh_key_path="/test", gateway_ssh_public_key="test")

    def card(lang, at=NOW, account=owner):
        html = bilingual.project_html(usersite.page(store, cfg, account, "session", at),
                                      lang, "/account")
        match = re.search(r'id="slot-mac-private-gemini"[^>]*>(.*?)</div>', html, re.S)
        return match[1] if match else ""

    def labels(english, chinese, at=NOW):
        for lang, words in (("en", english), ("zh", chinese)):
            rendered = card(lang, at)
            assert all(word in rendered for word in words)
            prefix = "/zh" if lang == "zh" else ""
            assert f'href="{prefix}/account/gemini#subscription"' in rendered

    # Claude enrollment alone must not count as a Gemini pairing.
    store.register_cli_device(store.request_cli_pairing("mac-private", "operator", now=NOW),
                              public_key(1), "Claude", now=NOW)
    labels(("No paired computers", "Pair a computer"), ("尚无已配对的电脑", "配对电脑"))
    assert not card("en", account=store.get_account("other-admin"))
    store.private_owner_access("mac-private", workspace_at=NOW)
    first = store.antigravity_pairing("operator", now=NOW)
    second = store.antigravity_pairing("operator", now=NOW)
    labels(("2 unused one-time codes", "Finish pairing"), ("2 个待使用的一次性配对码", "完成配对"))
    paired = store.register_cli_device(first, public_key(2), "Laptop", now=NOW,
                                       workspace_pairing=True)
    labels(("1 paired computer", "1 unused one-time code", "Manage Gemini"),
           ("已配对 1 台电脑", "1 个待使用的一次性配对码", "管理 Gemini"))
    assert second not in card("en")
    reusable = store.set_antigravity_enrollment("operator", expected_id="", now=NOW)
    store.register_cli_device(reusable, public_key(3), "Desk", now=NOW, workspace_pairing=True)
    labels(("2 paired computers", "Reusable setup code active", "Manage Gemini"),
           ("已配对 2 台电脑", "可重复使用的配对码已启用", "管理 Gemini"))
    assert reusable not in card("en")
    store.revoke_cli_device("mac-private", paired["device_id"], held_by="operator")
    labels(("1 paired computer", "Reusable setup code active"),
           ("已配对 1 台电脑", "可重复使用的配对码已启用"), at=NOW + 601)
    assert "unused one-time code" not in card("en", NOW + 601)
    store.set_private_owner_inference("mac-private", "operator", False, now=NOW + 601)
    assert not card("en")


def test_manage_reusable_subscription_code_via_web(private, monkeypatch):
    from ccfleetd import bilingual, usersite
    from tests.test_gateway_access import public_key
    from tests.test_private_owner_nodes import NOW

    store, _ = private
    enable(store)
    store.private_owner_access("mac-private", workspace_at=NOW)
    monkeypatch.setattr(ui.time, "time", lambda: NOW)
    cfg = Config(cookie_secret="fake-cookie-secret", public_url="https://fleet.example",
                 gateway_enabled=True, gateway_ssh_key_path="/test", gateway_ssh_public_key="test")
    owner = store.get_account("operator")
    csrf = usersite.csrf_for("session", cfg.cookie_secret)

    def setup():
        return ui.page(store, cfg, owner, "session").body

    def post(operation, **fields):
        return ui.act(store, cfg, owner, "session", "workspace",
                      {"operation": operation, "csrf": csrf, **fields}, NOW)

    def issued(result):
        assert result.status == 200
        return re.search(r'id="gemini-code">agy1-(ccf_enroll_[\w-]+)</pre>', result.body)[1]

    assert post("create").status == 409
    assert post("create", trust="1", enrollment_id="").status == 409
    created = post("create", trust="1")
    code = issued(created)
    for lang, text in (("en", "password manager"), ("zh", "密码管理器")):
        receipt = bilingual.project_html(created.body, lang, "/account/gemini/workspace")
        assert text in receipt and "ccfleet pair gemini" in receipt
        assert "agy1-" + code in receipt and 'id="copy-status"' in receipt
    assert post("create", trust="1").status == 409
    body = setup()
    current = Form(body).values["enrollment_id"]
    for lang, text in (("en", "Reusable setup code active"), ("zh", "可重复使用的配对码已启用")):
        html = bilingual.project_html(body, lang, "/account/gemini")
        assert text in html and code not in html
        assert "ccfleet pair gemini" in html and "ccfleet gemini --preview" not in html
        assert 'value="create"' not in html
        assert 'value="replace"' in html and 'value="disable"' in html
        assert ("Select the local environment" if lang == "en" else "选择本地环境") in html
        assert ("one argument per row" if lang == "en" else "每行一个") in html
        for detail in ("Settings → Providers → Add provider → Show more → Local ACP command",
                       "<code>command -v ccfleet</code>", "CCFleet Gemini", "Supervised",
                       "<li><code>gemini</code></li><li><code>--acp</code></li>"):
            assert detail in html
    paired = store.register_cli_device(code, public_key(4), "<personal>", now=NOW,
                                       workspace_pairing=True)
    assert "&lt;personal&gt;" in setup() and "<personal>" not in setup()
    assert f'name="device_id" value="{paired["device_id"]}"' in setup()
    assert 'action="/account/slots/mac-private/cli-revoke"' in setup()
    account = usersite.page(store, cfg, owner, "session", NOW)
    assert "Use a connected computer" not in account
    assert paired["device_id"] not in account
    assert post("replace", trust="1").status == 409
    assert post("replace", enrollment_id=current).status == 409
    replacement = issued(post("replace", trust="1", enrollment_id=current))
    assert replacement != code and replacement not in setup()
    assert post("replace", trust="1", enrollment_id=current).status == 409
    assert post("disable", trust="1", enrollment_id=current).status == 409
    current = Form(setup()).values["enrollment_id"]
    store.private_owner_access("mac-private")  # An offline slot can still close enrollment.
    assert 'value="replace"' not in setup()
    disabled = post("disable", trust="1", enrollment_id=current)
    assert disabled.status == 303 and disabled.location == "/account/gemini#subscription"
    assert "1 paired computer" in setup() and 'value="disable"' not in setup()
    assert "Reusable setup code active" not in setup()
    remove = usersite.act(store, cfg, owner, "/account/slots/mac-private/cli-revoke",
                          {"csrf": csrf, "device_id": paired["device_id"]}, NOW, "session")
    assert remove.status == 303 and "No paired computers" in setup()
