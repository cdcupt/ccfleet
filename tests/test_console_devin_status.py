"""Admin labels reuse holder-scoped Devin admission without changing authority."""
from __future__ import annotations

import base64
import http.client
import threading
import time
from dataclasses import replace

import pytest

from ccfleetd import consoleslots, slots
from ccfleetd.api import Context, build_server, csrf_token
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.passwords import hash_password
from tests import test_account_setup_flow as setup_helpers
from tests.test_gateway_provider_controls import native_report

NOW, Flow = setup_helpers.NOW, setup_helpers.Flow
setup_context = setup_helpers.setup_context


def render(c, *, now=NOW, configured=True):
    return consoleslots.section(c.store, "ADMIN_CSRF", now,
                                cfg=c.cfg if configured else None)


def state_cell(source, machine="machine-2"):
    page = Flow(source)
    row = page.find("div", data_machine=machine)[0]
    return next(item for item in page.inside(row, "div") if item.has_class("c-state"))


def test_claimed_Devin_uses_verified_actual_holder_without_changing_state(setup_context):
    c = setup_context
    before = list(c.store._conn.iterdump())
    source = render(c)
    state = state_cell(source)
    assert "Devin ready" in state.text and "Ready to sign in" not in state.text
    assert any(item.has_class("ok") for item in state.content if not isinstance(item, str))
    assert c.store.get_slot("devin")["state"] == slots.CLAIMED
    assert list(c.store._conn.iterdump()) == before
    assert "In use" in state_cell(source, "machine-1").text
    assert "Devin ready" not in state_cell(source, "machine-3").text
    for secret in (c.existing["key_token"], c.foreign["key_token"], "a" * 64,
                   c.binding["claim_id"]):
        assert secret not in source


@pytest.mark.parametrize("fault", [
    "no-binding", "expired", "future-observation", "identity", "capability", "revoked",
    "generation", "runtime", "native-version", "model-map", "contract", "gateway-disabled",
    "Devin-disabled", "node-disabled", "absent", "new-claim", "new-holder", "wrong-provider-owner",
])
def test_unverified_or_wrong_rental_never_has_a_green_Devin_label(setup_context, monkeypatch, fault):
    c, now = setup_context, NOW
    warning = True
    if fault == "no-binding":
        c.store._conn.execute("DELETE FROM gateway_provider_bindings WHERE slot_id='devin'")
        warning = False
    elif fault == "expired":
        now += 301
    elif fault == "future-observation":
        c.store._conn.execute("UPDATE gateway_provider_bindings SET observed_at=?", (NOW + 1,))
    elif fault == "identity":
        c.store.bind_gateway_provider("devin", "alice", "devin", c.binding["claim_id"],
            native_report(NOW, identity_verified=False, account_fp=None, capabilities={}),
            expected_generation=c.binding["generation"], now=NOW)
    elif fault == "capability":
        c.store._conn.execute("UPDATE gateway_provider_bindings SET capabilities='{}'")
    elif fault == "revoked":
        c.store._conn.execute("UPDATE gateway_provider_bindings SET revoked_at=?", (NOW,))
    elif fault == "generation":
        # A page read can race with a new trusted observation. Admission must
        # re-read the generation rather than accept the initial binding snapshot.
        read = c.store.gateway_provider_binding
        previous = read("devin", "alice", "devin")
        c.store._conn.execute("UPDATE gateway_provider_bindings SET generation=generation+1")
        first_read = True

        def stale_once(slot_id, account_id, provider):
            nonlocal first_read
            if slot_id == "devin" and first_read:
                first_read = False
                return previous
            return read(slot_id, account_id, provider)

        monkeypatch.setattr(c.store, "gateway_provider_binding", stale_once)
    elif fault == "runtime":
        c.cfg = replace(c.cfg, gateway_devin_adapter_revision="f" * 64)
    elif fault == "native-version":
        c.cfg = replace(c.cfg, gateway_devin_native_version="3000.0.1")
    elif fault == "model-map":
        c.cfg = replace(c.cfg, gateway_devin_model_map_digest="f" * 64)
    elif fault == "contract":
        c.store._conn.execute("UPDATE gateway_provider_bindings SET bridge_contract='unknown'")
    elif fault == "gateway-disabled":
        c.cfg = replace(c.cfg, gateway_enabled=False)
    elif fault == "Devin-disabled":
        c.cfg = replace(c.cfg, gateway_devin_enabled=False)
    elif fault == "node-disabled":
        c.store.set_enabled("machine-2", False)
    elif fault == "absent":
        c.store._conn.execute("UPDATE slots SET present=0 WHERE id='devin'")
    elif fault == "new-claim":
        c.store._conn.execute("UPDATE slots SET gateway_claim_id=? WHERE id='devin'", ("f" * 32,))
        warning = False
    elif fault == "new-holder":
        c.store._conn.execute("UPDATE slots SET held_by='bob' WHERE id='devin'")
        warning = False
    else:
        c.store._conn.execute("UPDATE gateway_provider_bindings SET owner_account_id='bob'")
        warning = False
    c.store._conn.commit()
    before = list(c.store._conn.iterdump())
    state = state_cell(render(c, now=now))
    assert "Devin ready" not in state.text
    assert ("Devin needs a check" in state.text) is warning
    assert list(c.store._conn.iterdump()) == before


def test_legacy_calls_and_Claude_only_slots_keep_their_status(setup_context):
    c = setup_context
    assert "Ready to sign in" in state_cell(render(c, configured=False)).text
    c.store._conn.execute("DELETE FROM gateway_provider_bindings WHERE slot_id='devin'")
    c.store._conn.commit()
    assert "Ready to sign in" in state_cell(render(c)).text
    assert "In use" in state_cell(render(c), "machine-1").text


@pytest.mark.parametrize("replacement", ["claim", "holder"])
def test_a_concurrent_rental_change_cannot_inherit_a_previous_ready_label(
        setup_context, monkeypatch, replacement):
    c = setup_context
    list_slots = c.store.list_slots
    changed = False

    def changed_rows(*args, **kwargs):
        nonlocal changed
        if kwargs.get("node_id") == "machine-2" and not changed:
            changed = True
            if replacement == "claim":
                c.store._conn.execute("UPDATE slots SET gateway_claim_id=? WHERE id='devin'",
                                      ("f" * 32,))
            else:
                c.store._conn.execute("UPDATE slots SET held_by='bob' WHERE id='devin'")
            c.store._conn.commit()
        return list_slots(*args, **kwargs)

    monkeypatch.setattr(c.store, "list_slots", changed_rows)
    state = state_cell(render(c))
    assert changed and "Devin ready" not in state.text
    assert "Ready to sign in" in state.text


@pytest.mark.parametrize("reboot", [True, False])
def test_machine_reboot_warning_is_independent_of_Devin_login(setup_context, reboot):
    c = setup_context
    c.store.insert_heartbeat("machine-2", NOW + 1,
        {"node_id": "machine-2", "mode": "machine", "reboot_required": reboot,
         "slots": [{"unix_user": "slot01", "claude": {"version": "2.1.303"},
                    "credentials": {"logged_in": False}}]})
    source = render(c)
    assert "Devin ready" in state_cell(source).text
    page = Flow(source)
    row = page.find("div", data_machine="machine-2")[0]
    assert ("reboot needed" in row.text) is reboot
    assert "2.1.303" in row.text


def test_admin_GET_preserves_authority_forms_and_console_role_boundaries(setup_context):
    c, moment = setup_context, time.time()
    cfg = replace(c.cfg, bind_port=0, admin_token="SYNTHETIC_ADMIN_TOKEN", cookie_secure=False)
    c.store.bind_gateway_provider("devin", "alice", "devin", c.binding["claim_id"],
        native_report(moment), expected_generation=c.binding["generation"], now=moment)
    c.store.add_user("owner", hash_password("synthetic-owner-password"), "owner", "operator", moment)
    server = build_server(Context(c.store, cfg, Monitor(c.store, cfg, LogNotifier())),
                          host="127.0.0.1", port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def get(credentials=None):
        headers = ({"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}
                   if credentials else {})
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("GET", "/admin", headers=headers)
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    try:
        before = list(c.store._conn.iterdump())
        code, source = get("admin:SYNTHETIC_ADMIN_TOKEN")
        assert code == 200 and "Devin ready" in state_cell(source).text
        assert list(c.store._conn.iterdump()) == before
        page = Flow(source)
        forms = page.find("form")
        assert any(form.attrs.get("action") == "/actions/slot/devin/reclaim" for form in forms)
        for form in forms:
            if form.attrs.get("method", "").lower() == "post":
                fields = [item for item in page.inside(form, "input")
                          if item.attrs.get("name") == "csrf"]
                assert len(fields) == 1 and fields[0].attrs["value"] == csrf_token(cfg)
        _, owner = get("owner:synthetic-owner-password")
        assert 'id="slots"' not in owner and "alice@example.invalid" not in owner
        _, stranger = get()
        assert 'id="slots"' not in stranger and "alice@example.invalid" not in stranger
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
