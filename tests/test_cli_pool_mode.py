"""A configured slot pool and a fixed local choice have different, visible semantics."""
from __future__ import annotations

import copy
from urllib.parse import urlsplit

import pytest

from tests.test_gateway_client import SECRET, invoke, profile
from tests.test_gateway_client import client as client
from tests.test_gateway_slot_discovery import discovery as discovery
from tests.test_gateway_slot_discovery import enrollments, network, setup


def pin(client, identifier="fresh-slot", name="reused"):
    config = client.loaded["load_config"]()
    client.loaded["remember_local_slot"](config, client.device, slot_id=identifier, slot_name=name)
    client.loaded["save_config"](config)
    return copy.deepcopy(config)


@pytest.mark.parametrize("mode", ["sticky", "priority"])
def test_use_pool_clears_only_override_and_launches_without_slot_header(
        discovery, monkeypatch, capsys, mode):
    profile(discovery, slot_ids=["slot-one", "fresh-slot"], mode=mode)
    calls, state = network(discovery, monkeypatch, current_ids=["slot-one", "fresh-slot"])
    state["mode"] = mode
    before = pin(discovery)
    key_path = discovery.loaded["home"]() / "gateway.json"
    key_bytes = key_path.read_bytes()
    assert discovery.loaded["main"](["use", "--pool"]) == 0
    after = discovery.loaded["load_config"]()
    assert after == {key: value for key, value in before.items() if key != "local_slot_choice"}
    assert key_path.read_bytes() == key_bytes
    assert invoke(discovery, "--check") == 0
    check = capsys.readouterr()
    assert "Selected route: configured slot pool" in check.err
    assert "Routing: " + mode in check.err and "Slot order: one → reused" in check.err
    assert "automatic switching is disabled" not in check.err
    assert not discovery.calls
    assert invoke(discovery) == 0
    assert discovery.calls[0][3]["env"]["ANTHROPIC_CUSTOM_HEADERS"] == ""
    assert all(call[3] == "GET" and not urlsplit(call[0]).query for call in calls)
    output = capsys.readouterr()
    assert "Routing: " + mode in output.err
    assert SECRET not in output.out + output.err + check.out + check.err


@pytest.mark.parametrize("failure", ["missing", "denied", "wrong_endpoint", "bad_response"])
def test_use_pool_cannot_clear_pin_before_validating_existing_endpoint(
        discovery, monkeypatch, failure):
    before = pin(discovery)
    if failure != "missing":
        saved = profile(discovery)
        if failure == "wrong_endpoint":
            profile(discovery, base_url="https://other.invalid")
    def refused(*args, **kwargs):
        if failure in {"missing", "wrong_endpoint"}:
            pytest.fail("invalid endpoint contacted")
        if failure == "bad_response":
            return {"ready": False, "key": {"id": "wrong-key", "slot_ids": ["slot-one"],
                                            "mode": saved["mode"]}}
        raise discovery.loaded["CliError"]("synthetic denied", status=403)
    monkeypatch.setitem(discovery.scope, "api_json", refused)
    assert discovery.loaded["main"](["use", "--pool"]) == 2
    assert discovery.loaded["load_config"]() == before
    assert not discovery.calls


@pytest.mark.parametrize("reuse", [False, True])
def test_explicit_gateway_pool_setup_clears_old_pin_after_success(
        discovery, monkeypatch, capsys, reuse):
    chosen = ["slot-one", "fresh-slot"] if reuse else ["slot-one"]
    profile(discovery, slot_ids=chosen)
    calls, _ = network(discovery, monkeypatch, current_ids=chosen)
    before = pin(discovery)
    assert setup(discovery, "--slots", "one", "reused", "--routing", "sticky") == 0
    after = discovery.loaded["load_config"]()
    assert after == {key: value for key, value in before.items() if key != "local_slot_choice"}
    assert bool(enrollments(calls)) is not reuse
    assert "previous single-slot pin was cleared" in capsys.readouterr().out
    assert invoke(discovery, "--check") == 0
    assert "Selected route: configured slot pool" in capsys.readouterr().err
    if not reuse:
        assert discovery.loaded["load_gateway"]()["slot_names"] == {
            "slot-one": "one", "fresh-slot": "reused"}


def test_gateway_setup_without_explicit_slots_keeps_and_explains_fixed_choice(
        discovery, monkeypatch, capsys):
    profile(discovery, slot_ids=["slot-one"])
    network(discovery, monkeypatch)
    before = pin(discovery, "slot-one", "one")
    assert setup(discovery) == 0
    assert discovery.loaded["load_config"]() == before
    output = capsys.readouterr().out
    assert "Local route remains fixed to one" in output and "ccfleet use --pool" in output


def test_failed_pool_setup_keeps_previous_fixed_choice(discovery, monkeypatch):
    profile(discovery, slot_ids=["slot-one"])
    network(discovery, monkeypatch)
    before = pin(discovery)
    def pending(*args):
        raise discovery.loaded["CliError"]("synthetic readiness pending")
    monkeypatch.setitem(discovery.scope, "wait_for_gateway_setup", pending)
    assert setup(discovery, "--slots", "one", "reused") == 2
    assert discovery.loaded["load_config"]() == before


def test_offline_list_describes_pool_order_and_pairings_without_star_or_internal_ids(
        discovery, monkeypatch, capsys):
    profile(discovery, slot_ids=["pool-41", "pool-92"],
            slot_names={"pool-41": "slot-1234", "pool-92": "slot-5678"})
    monkeypatch.setitem(discovery.scope, "api_json", lambda *a, **k: pytest.fail("offline list"))
    assert discovery.loaded["main"](["list"]) == 0
    output = capsys.readouterr().out
    assert "Local route: configured slot pool (no single-slot pin)." in output
    assert "Routing: sticky" in output and "Slot order: slot-1234 → slot-5678" in output
    assert "Saved computer pairings:" in output
    assert "pool-41" not in output and "pool-92" not in output and "*" not in output


@pytest.mark.parametrize("legacy_name", ["pool-92", "", "\x1b[2JPRIVATE", "x" * 201])
def test_offline_list_does_not_publish_unknown_or_unsafe_legacy_pairing_names(
        discovery, monkeypatch, capsys, legacy_name):
    config = discovery.loaded["load_config"]()
    config["devices"]["device"].update(slot_id="pool-92", slot_name=legacy_name)
    discovery.loaded["save_config"](config)
    profile(discovery, slot_ids=["pool-92"])
    monkeypatch.setitem(discovery.scope, "api_json", lambda *a, **k: pytest.fail("offline list"))
    assert discovery.loaded["main"](["list"]) == 0
    output = capsys.readouterr().out
    assert "Name unavailable" in output and "ccfleet list --remote" in output
    assert "pool-92" not in output and "PRIVATE" not in output and "x" * 201 not in output


@pytest.mark.parametrize("nickname", ["pool-backup", "pool-a"])
def test_customer_chosen_pool_nicknames_remain_visible(discovery, monkeypatch, capsys, nickname):
    config = discovery.loaded["load_config"]()
    config["devices"]["device"].update(slot_id="pool-92", slot_name=nickname)
    discovery.loaded["save_config"](config)
    profile(discovery, slot_ids=["pool-92"])
    monkeypatch.setitem(discovery.scope, "api_json", lambda *a, **k: pytest.fail("offline list"))
    assert discovery.loaded["main"](["list"]) == 0
    output = capsys.readouterr().out
    assert "Slot order: " + nickname in output and "  " + nickname in output
    assert "pool-92" not in output


@pytest.mark.parametrize("saved_choice", [False, True])
@pytest.mark.parametrize("server", ["https://ccfleet.io", "https://ccfleet.daichenlab.com"])
def test_cached_gateway_names_are_scoped_to_canonical_endpoint(
        discovery, monkeypatch, capsys, saved_choice, server):
    config = discovery.loaded["load_config"]()
    config["devices"]["device"].update(server=server, slot_id="pool-92", slot_name="slot-5678")
    config["devices"]["foreign"] = {**discovery.device, "device_id": "foreign",
        "server": "https://other.invalid", "slot_id": "pool-92", "slot_name": "slot-9999"}
    if saved_choice:
        config["local_slot_choice"] = {"device_id": "foreign", "slot_id": "pool-92",
                                       "slot_name": "slot-8888"}
    discovery.loaded["save_config"](config)
    saved = profile(discovery, slot_ids=["pool-92"], base_url="https://ccfleet.io")
    monkeypatch.setitem(discovery.scope, "api_json", lambda *a, **k: pytest.fail("offline list"))
    assert discovery.loaded["gateway_slot_labels"](saved, config=config) == ["slot-5678"]
    assert discovery.loaded["main"](["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    order = next(line for line in lines if "slot order:" in line.lower())
    assert order.endswith("slot-5678") and "slot-9999" not in order and "slot-8888" not in order


@pytest.mark.parametrize("gateway_failure", ["revoked", "unavailable", "different_endpoint"])
def test_remote_slot_inventory_survives_saved_gateway_failure(
        discovery, monkeypatch, capsys, gateway_failure):
    profile(discovery, slot_ids=["slot-one"], mode="priority",
            base_url="https://other.invalid" if gateway_failure == "different_endpoint"
            else "https://fleet.invalid")
    calls, _ = network(discovery, monkeypatch)
    original = discovery.scope["api_json"]
    def api(url, token, *args, **kwargs):
        if urlsplit(url).path == "/api/gateway/status":
            assert gateway_failure != "different_endpoint", "unrelated endpoint contacted"
            raise discovery.loaded["CliError"]("private reflected " + SECRET,
                                               status=401 if gateway_failure == "revoked" else 503)
        return original(url, token, *args, **kwargs)
    monkeypatch.setitem(discovery.scope, "api_json", api)
    key = discovery.loaded["home"]() / "gateway.json"
    before = [discovery.loaded["config_path"]().read_bytes(), key.read_bytes()]
    assert discovery.loaded["main"](["list", "--remote"]) == 0
    output = capsys.readouterr().out
    assert "Your current slots:" in output and "  reused" in output and "  other" in output
    assert "Routing:" not in output and "Slot order:" not in output
    assert "pool priority" not in output and SECRET not in output
    assert ("another connection" if gateway_failure == "different_endpoint"
            else "Gateway routing is unavailable") in output
    assert len(calls) == 1 and calls[0][0].endswith("/api/cli/gateway/options")
    assert [discovery.loaded["config_path"]().read_bytes(), key.read_bytes()] == before


def test_remote_list_refreshes_names_and_order_without_rewriting_state(
        discovery, monkeypatch, capsys):
    offered = [{"id": "pool-41", "name": "slot-1234"},
               {"id": "pool-92", "name": "slot-5678"}]
    profile(discovery, slot_ids=["pool-92"], mode="priority",
            slot_names={"pool-92": "old-name"})
    calls, state = network(discovery, monkeypatch, available_slots=offered,
                           current_ids=["pool-41", "pool-92"])
    state["mode"] = "sticky"
    config_path = discovery.loaded["config_path"]()
    key_path = discovery.loaded["home"]() / "gateway.json"
    before = [config_path.read_bytes(), key_path.read_bytes()]
    assert discovery.loaded["main"](["list", "--remote"]) == 0
    output = capsys.readouterr().out
    assert "Slot order: slot-1234 → slot-5678" in output and "Routing: sticky" in output
    assert "slot-1234 — pool priority 1" in output and "slot-5678 — pool priority 2" in output
    assert "pool-41" not in output and "pool-92" not in output and "old-name" not in output
    assert all(call[3] == "GET" for call in calls)
    assert [config_path.read_bytes(), key_path.read_bytes()] == before


def test_gateway_status_and_launch_explain_fixed_override_of_sticky_pool(
        discovery, monkeypatch, capsys):
    profile(discovery, slot_ids=["slot-one", "fresh-slot"])
    network(discovery, monkeypatch, current_ids=["slot-one", "fresh-slot"])
    pin(discovery)
    assert discovery.loaded["main"](["gateway", "status"]) == 0
    output = capsys.readouterr().out
    assert "Configured gateway routing: sticky (overridden by fixed slot)" in output
    assert "Local route: fixed to reused" in output
    assert "pool routing is overridden" in output and "ccfleet use --pool" in output
    assert invoke(discovery, "--check") == 0
    output = capsys.readouterr().err
    assert "Selected route: reused" in output and "local slot pin overrides gateway pool" in output
    assert "Model route: sticky." not in output
    assert "Configured gateway routing: sticky (overridden by fixed slot)" in output
    assert "\nRouting: sticky" not in output


@pytest.mark.parametrize("labels", [[], {"slot-one": "\x1b[2J" + SECRET},
                                    {"../invalid": "one"}, {"slot-one": 1},
                                    {"unselected-slot": "other"}])
def test_untrusted_cached_display_names_cannot_reach_terminal(client, labels, capsys):
    with pytest.raises(client.loaded["CliError"]):
        profile(client, slot_names=labels)
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err


def test_gateway_status_prunes_cached_names_for_slots_removed_on_website(
        discovery, monkeypatch, capsys):
    profile(discovery, slot_ids=["slot-one", "fresh-slot"],
            slot_names={"slot-one": "old-one", "fresh-slot": "old-other"})
    network(discovery, monkeypatch, current_ids=["fresh-slot"])
    saved = discovery.loaded["load_gateway"]()
    discovery.loaded["check_gateway"](saved)
    assert saved["slot_ids"] == ["fresh-slot"]
    assert saved["slot_names"] == {"fresh-slot": "old-other"}
    assert discovery.loaded["main"](["gateway", "status"]) == 0
    output = capsys.readouterr().out
    assert "Slot order: reused" in output and "old-one" not in output


@pytest.mark.parametrize("arguments", [["use"], ["use", "one", "--pool"]])
def test_use_requires_exactly_one_selection(client, arguments):
    with pytest.raises(SystemExit) as rejected:
        client.loaded["main"](arguments)
    assert rejected.value.code == 2
