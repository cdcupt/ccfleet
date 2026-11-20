"""Browser-to-Mac credential enrollment without sending a provider key to Fleet."""

import json
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
