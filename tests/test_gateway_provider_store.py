"""Dormant native provider ownership; no native process or dispatch is registered."""

import json
import re
import sqlite3
from dataclasses import replace

import pytest

from ccfleetd import gateway_ui, slots
from ccfleetd.gateway import GatewayRuntime
from ccfleetd.store import SCHEMA, NotYours, Store, StoreError
from tests.test_gateway_access import add_held_slot
from tests.test_gateway_access import owner as owner
from tests.test_gateway_providers import NOW, report


@pytest.fixture
def prepared(owner):
    owner._conn.execute("UPDATE slots SET present = 1")
    owner._conn.commit()
    return owner


def bind(store, slot="s1", account="a1", provider="devin", *, generation=0, **changes):
    claim = store.get_slot(slot)["gateway_claim_id"]
    return store.bind_gateway_provider(slot, account, provider, claim, report(**changes),
                                        expected_generation=generation, now=NOW + 1)


def test_existing_default_gateway_keys_need_no_new_evidence(prepared):
    key = prepared.create_gateway_key("a1", "Existing", ["s1", "s2"], now=NOW)
    assert key["provider"] == "anthropic"
    assert len(prepared.gateway_targets(key)) == 2
    assert prepared.resolve_gateway_key(key["key_token"], now=NOW + 1)["provider"] == "anthropic"
    assert prepared._conn.execute("SELECT COUNT(*) FROM gateway_provider_bindings").fetchone()[0] == 0


@pytest.mark.parametrize("enabled,evidence", [(False, None), (True, None), (False, {})])
def test_devin_requires_both_explicit_flag_and_verified_provider_binding(prepared, enabled, evidence):
    if evidence is not None:
        bind(prepared)
    with pytest.raises(StoreError):
        prepared.create_gateway_key("a1", "Dormant", ["s1"], provider="devin",
                                     allow_devin=enabled, now=NOW + 1)
    assert prepared.list_gateway_keys("a1") == []


def test_pong_partial_or_missing_identity_never_admits_provider_selection(prepared):
    binding = bind(prepared, account_fp=None, identity_verified=False,
                   capabilities={"messages": True})
    with pytest.raises(StoreError):
        prepared.create_gateway_key("a1", "Not ready", ["s1"], provider="devin",
                                     allow_devin=True, now=NOW + 1)
    assert not prepared.gateway_provider_binding_is_active(
        "s1", "a1", "devin", binding["claim_id"], binding["generation"], now=NOW + 1)


def test_provider_edit_keeps_token_but_invalidates_old_generation_and_session_state(prepared, cfg):
    made = prepared.create_gateway_key("a1", "Same token", ["s1"], now=NOW)
    old = prepared.resolve_gateway_key(made["key_token"], now=NOW)
    [old_target] = prepared.gateway_targets(old)
    binding = bind(prepared)
    runtime = GatewayRuntime(replace(cfg, broker_only=True), prepared,
        transport_factory=lambda *_: (_ for _ in ()).throw(AssertionError("unexpected dispatch")))
    try:
        session = runtime._session_key(old, "same-conversation")
        runtime.affinity[session] = ("s1", old_target["claim_generation"], 9999999999)
        runtime.health[("s1", old_target["claim_generation"], old_target["access_host"],
                        old_target["access_port"], old_target["ssh_host_key"])] = (True, 9999999999)
        changed = prepared.update_gateway_key(old["id"], "a1", ["s1"], "sticky", provider="devin",
                                               allow_devin=True, now=NOW + 1)
        current = prepared.resolve_gateway_key(made["key_token"], now=NOW + 1)
        assert current["id"] == old["id"] and current["provider"] == "devin"
        assert current["generation"] == old["generation"] + 1 == changed["generation"]
        assert runtime._session_key(current, "same-conversation") != session
        assert not prepared.gateway_request_is_active(old["id"], old["generation"], "s1",
                                                       old_target["claim_generation"])
        # Even full synthetic metadata and a cached Anthropic health result
        # cannot register the absent native transport.
        assert prepared.gateway_targets(current) == []
        assert runtime.status(current)["ready"] is False
        assert binding["generation"] == 1
        reverted = prepared.update_gateway_key(old["id"], "a1", ["s1"], "sticky",
                                                 provider="anthropic", now=NOW + 2)
        assert reverted["generation"] == current["generation"] + 1
        assert prepared.resolve_gateway_key(made["key_token"], now=NOW + 2)["provider"] == "anthropic"
        assert len(prepared.gateway_targets(reverted)) == 1
    finally:
        runtime.close()


def test_omitted_provider_update_preserves_existing_choice_instead_of_resetting_it(prepared):
    bind(prepared)
    key = prepared.create_gateway_key("a1", "Dormant", ["s1"], provider="devin",
                                       allow_devin=True, now=NOW + 1)
    changed = prepared.update_gateway_key(key["id"], "a1", ["s1"], "sticky", name="Renamed",
                                           allow_devin=True, now=NOW + 1)
    assert changed["provider"] == "devin" and changed["generation"] == key["generation"] + 1
    with pytest.raises(StoreError):
        prepared.update_gateway_key(key["id"], "a1", ["s1"], "sticky", now=NOW + 1)


@pytest.mark.parametrize("provider", [None, "", "Devin", "../devin", "native", [], True])
def test_unknown_provider_is_rejected_without_creating_keys(prepared, provider):
    with pytest.raises(StoreError):
        prepared.create_gateway_key("a1", "Invalid", ["s1"], provider=provider, now=NOW)
    assert prepared.list_gateway_keys("a1") == []


def test_provider_selection_is_single_slot_and_never_uses_another_owner(prepared):
    bind(prepared)
    bind(prepared, "s2", account_fp="e" * 64)
    for selected in (["s1", "s2"], ["s3"]):
        with pytest.raises(StoreError):
            prepared.create_gateway_key("a1", "Invalid", selected, provider="devin",
                                         allow_devin=True, now=NOW + 1)
    with pytest.raises(NotYours):
        prepared.bind_gateway_provider("s1", "a2", "devin",
            prepared.get_slot("s1")["gateway_claim_id"], report(), now=NOW + 1)
    with pytest.raises(StoreError):
        prepared.bind_gateway_provider("s1", "a1", "devin", "0" * 32, report(), now=NOW + 1)


def test_verified_provider_identity_cannot_be_pooled_across_held_slots(prepared):
    bind(prepared)
    with pytest.raises(StoreError, match="already bound"):
        bind(prepared, "s2")
    # Fingerprints from different providers live in different namespaces.
    assert bind(prepared, "s2", provider="anthropic")["provider"] == "anthropic"


def test_binding_refresh_keeps_generation_but_account_or_adapter_change_revokes_old_state(prepared):
    first = bind(prepared)
    anthropic = prepared.create_gateway_key("a1", "Claude", ["s1"], now=NOW)
    devin = prepared.create_gateway_key("a1", "Native", ["s1"], provider="devin",
                                         allow_devin=True, now=NOW + 1)
    refreshed = bind(prepared, generation=1, observed_at=NOW + 1)
    assert refreshed["generation"] == first["generation"]
    assert prepared.list_gateway_keys("a1")[1]["generation"] == devin["generation"]
    changed = bind(prepared, generation=1, observed_at=NOW + 1, adapter_revision="e" * 64)
    assert changed["generation"] == first["generation"] + 1
    current = prepared.resolve_gateway_key(devin["key_token"], now=NOW + 1)
    assert current["generation"] == devin["generation"] + 1
    assert prepared.resolve_gateway_key(anthropic["key_token"], now=NOW + 1)[
        "generation"] == anthropic["generation"]
    assert not prepared.gateway_provider_binding_is_active(
        "s1", "a1", "devin", first["claim_id"], first["generation"], now=NOW + 1)
    assert prepared.gateway_provider_binding_is_active(
        "s1", "a1", "devin", changed["claim_id"], changed["generation"], now=NOW + 1)
    with pytest.raises(StoreError, match="generation"):
        bind(prepared, generation=1, observed_at=NOW + 1)


def test_revocation_invalidates_binding_and_key_generations_without_rotating_secret(prepared):
    binding = bind(prepared)
    key = prepared.create_gateway_key("a1", "Native", ["s1"], provider="devin",
                                       allow_devin=True, now=NOW + 1)
    assert prepared.revoke_gateway_provider_binding("s1", "a1", "devin", binding["claim_id"],
                                                     binding["generation"], now=NOW + 1)
    assert not prepared.gateway_provider_binding_is_active(
        "s1", "a1", "devin", binding["claim_id"], binding["generation"], now=NOW + 1)
    assert prepared.resolve_gateway_key(key["key_token"], now=NOW + 1)[
        "generation"] == key["generation"] + 1
    with pytest.raises(StoreError):
        bind(prepared, generation=1, observed_at=NOW + 1)
    with pytest.raises(StoreError):
        prepared.update_gateway_key(key["id"], "a1", ["s1"], "sticky", provider="devin",
                                      allow_devin=True, now=NOW + 1)


def test_old_rental_binding_cannot_be_reused_after_same_owner_reclaims(prepared):
    binding = bind(prepared)
    prepared.begin_release("s1", held_by="a1")
    assert not prepared.gateway_provider_binding_is_active(
        "s1", "a1", "devin", binding["claim_id"], binding["generation"], now=NOW + 1)
    prepared.finish_release("s1", now=NOW + 1)
    assert prepared._conn.execute(
        "SELECT COUNT(*) FROM gateway_provider_bindings WHERE slot_id = 's1'").fetchone()[0] == 0
    prepared.apply_slot_report("m1", [{"unix_user": "slot01", "present": False}], now=NOW + 1)
    prepared.claim_slot("a1", node_id="m1", now=NOW + 2)
    prepared.move_slot("s1", slots.CLAIMED)
    prepared.move_slot("s1", slots.ACTIVE)
    assert prepared.gateway_provider_binding("s1", "a1", "devin") is None
    with pytest.raises(StoreError):
        prepared.bind_gateway_provider("s1", "a1", "devin", binding["claim_id"], report(),
                                         expected_generation=1, now=NOW + 2)
    with pytest.raises(StoreError):
        prepared.create_gateway_key("a1", "New rental", ["s1"], provider="devin",
                                       allow_devin=True, now=NOW + 2)


def test_slot_rename_preserves_current_binding_and_expires_key_snapshot(prepared):
    binding = bind(prepared)
    key = prepared.create_gateway_key("a1", "Native", ["s1"], provider="devin",
                                       allow_devin=True, now=NOW + 1)
    # Stale metadata under a retired target name must never be adopted.
    prepared._conn.execute(
        "INSERT INTO gateway_provider_bindings SELECT 'renamed', provider, owner_account_id, "
        "claim_id, account_fp, generation + 20, identity_verified, bridge_contract, capabilities, "
        "native_version, adapter_revision, model_map_digest, observed_at, revoked_at "
        "FROM gateway_provider_bindings WHERE slot_id = 's1'")
    prepared._conn.commit()
    prepared.rename_slot("s1", "renamed")
    moved = prepared.gateway_provider_binding("renamed", "a1", "devin")
    assert moved == {**binding, "slot_id": "renamed"}
    current = prepared.resolve_gateway_key(key["key_token"], now=NOW + 1)
    assert current["slot_ids"] == ["renamed"]
    assert current["generation"] == key["generation"] + 1
    assert prepared.gateway_provider_binding_is_active(
        "renamed", "a1", "devin", binding["claim_id"], binding["generation"], now=NOW + 1)
    assert not prepared.gateway_provider_binding_is_active(
        "s1", "a1", "devin", binding["claim_id"], binding["generation"], now=NOW + 1)


def test_removing_free_slot_discards_any_stale_provider_metadata(prepared):
    binding = bind(prepared)
    prepared.begin_release("s1", held_by="a1")
    prepared.finish_release("s1", now=NOW + 1)
    # Defend against older or manually restored orphan metadata too.
    columns = tuple(binding)
    prepared._conn.execute(
        f"INSERT INTO gateway_provider_bindings ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})",
        tuple(json.dumps(binding[key]) if key == "capabilities" else binding[key]
              for key in columns))
    prepared._conn.commit()
    prepared.remove_slot("s1")
    prepared.add_slot("s1", "m1", "slot01", now=NOW + 2)
    assert prepared._conn.execute(
        "SELECT COUNT(*) FROM gateway_provider_bindings WHERE slot_id = 's1'").fetchone()[0] == 0


def test_no_report_stale_report_disabled_machine_or_forged_owner_is_ready(prepared):
    binding = bind(prepared)
    with pytest.raises(StoreError):
        prepared.create_gateway_key("a1", "Stale", ["s1"], provider="devin", allow_devin=True,
                                       now=NOW + 1000)
    prepared.set_enabled("m1", False)
    with pytest.raises(StoreError):
        prepared.create_gateway_key("a1", "Paused", ["s1"], provider="devin", allow_devin=True,
                                       now=NOW + 1)
    assert not prepared.gateway_provider_binding_is_active(
        "s1", "a2", "devin", binding["claim_id"], 1, now=NOW + 1)


def test_current_ui_has_no_native_provider_selector_even_with_synthetic_ready_metadata(prepared, cfg):
    bind(prepared)
    configured = replace(cfg, public_url="https://fleet.example.com", gateway_enabled=True,
        gateway_devin_enabled=True, gateway_ssh_key_path="/unused", gateway_ssh_public_key="unused")
    page = gateway_ui.panel(prepared, configured, {"id": "a1"}, "csrf",
                            prepared.list_slots(held_by="a1"))
    assert 'name="provider"' not in page and 'value="devin"' not in page


def test_legacy_key_migration_defaults_anthropic_without_changing_secret_or_generation(tmp_path):
    path = tmp_path / "legacy.db"
    store = Store(str(path))
    store.add_account("a1", "subject", "synthetic@example.test", slot_quota=1, now=1)
    add_held_slot(store, 1)
    made = store.create_gateway_key("a1", "Legacy", ["s1"], now=NOW)
    store.close()
    connection = sqlite3.connect(path)
    columns = [row[1] for row in connection.execute("PRAGMA table_info(gateway_keys)")
               if row[1] != "provider"]
    old_definition = re.search(r"CREATE TABLE IF NOT EXISTS gateway_keys \(.*?\n\);", SCHEMA,
                               re.S)[0]
    old_definition = re.sub(r"\n    provider TEXT[^\n]+", "", old_definition)
    connection.execute("ALTER TABLE gateway_keys RENAME TO old_future_keys")
    connection.execute(old_definition)
    names = ",".join(columns)
    connection.execute(f"INSERT INTO gateway_keys ({names}) SELECT {names} FROM old_future_keys")
    connection.execute("DROP TABLE old_future_keys")
    connection.commit()
    connection.close()
    reopened = Store(str(path))
    try:
        current = reopened.resolve_gateway_key(made["key_token"], now=NOW + 1)
        assert current["provider"] == "anthropic"
        assert current["id"] == made["id"] and current["generation"] == made["generation"]
        assert len(reopened.gateway_targets(current)) == 1
        assert reopened._conn.execute(
            "SELECT COUNT(*) FROM gateway_provider_bindings").fetchone()[0] == 0
    finally:
        reopened.close()


def test_binding_reports_cannot_store_credentials_and_gateway_tokens_stay_hashed(prepared):
    with pytest.raises(StoreError):
        bind(prepared, token="synthetic-secret")
    assert prepared._conn.execute("SELECT COUNT(*) FROM gateway_provider_bindings").fetchone()[0] == 0
    made = prepared.create_gateway_key("a1", "Existing", ["s1"], now=NOW)
    serialized = json.dumps([dict(row) for row in prepared._conn.execute(
        "SELECT * FROM gateway_keys")])
    assert made["key_token"] not in serialized
