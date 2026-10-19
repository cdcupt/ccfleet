"""Fresh HTTPS slot/provider discovery using fake network and disposable state."""
from __future__ import annotations

import copy
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_gateway_client import SECRET, invoke, profile
from tests.test_gateway_client import client as client


@pytest.fixture
def discovery(client, monkeypatch):
    personal = client.project / "synthetic-home"
    personal.mkdir()
    monkeypatch.setenv("HOME", str(personal))
    monkeypatch.setitem(client.scope, "SETUP_WAIT_SECONDS", 0.01)
    monkeypatch.setitem(client.scope, "native_proxy_exclusions", lambda *args: None)
    key, pin = client.project / "paired.key", client.project / "paired.pin"
    key.write_bytes(b"SYNTHETIC_PAIRED_KEY_KEEP_EXACT\n")
    pin.write_bytes(b"SYNTHETIC_HOST_PIN_KEEP_EXACT\n")
    key.chmod(0o600)
    pin.chmod(0o600)
    client.device.update(key=str(key), known_hosts=str(pin))
    retired = {**client.device, "device_id": "retired", "slot_id": "old-slot",
               "slot_name": "reused", "device_token": "synthetic-old-device"}
    client.loaded["save_config"]({"devices": {"device": client.device, "retired": retired},
                                  "active": "device"})
    client.pairing_bytes = {path: path.read_bytes() for path in
                            (client.loaded["config_path"](), key, pin)}
    client.retired_grants = []
    monkeypatch.setitem(client.scope, "retire_live_grants",
                        lambda device, **kwargs: client.retired_grants.append(device["device_id"]))
    return client


def pairings_unchanged(client):
    assert all(path.read_bytes() == raw for path, raw in client.pairing_bytes.items())


def slots():
    return [{"id": "slot-one", "name": "one", "state": "active",
             "providers": ["anthropic", "devin"]},
            {"id": "fresh-slot", "name": "reused", "state": "claimed",
             "providers": ["anthropic", "devin"]},
            {"id": "other-slot", "name": "other", "state": "claimed",
             "providers": ["anthropic"]}]


def network(client, monkeypatch, *, available_slots=None, current_provider="anthropic",
            current_ids=None, returned_provider=None, options_providers=None, ready_before=True):
    calls = []
    state = {"provider": current_provider, "id": "gsynthetic", "slot_ids": current_ids or ["slot-one"],
             "mode": "sticky"}
    offered = slots() if available_slots is None else available_slots

    def api(url, token, payload=None, method="POST", **kwargs):
        calls.append((url, token, copy.deepcopy(payload), method))
        path = urlsplit(url).path
        if path == "/api/cli/gateway/options":
            assert token == client.device["device_token"] and method == "GET" and payload is None
            return {"available": True, "base_url": "https://fleet.invalid",
                    "providers": ["anthropic", "devin"] if options_providers is None else options_providers,
                    "slots": copy.deepcopy(offered)}
        if path == "/api/cli/gateway":
            assert token == client.device["device_token"] and method == "POST"
            provider = payload.get("provider", "anthropic") if returned_provider is None else returned_provider
            state.update(provider=provider, id="gnew", slot_ids=payload["slot_ids"], mode=payload["mode"])
            return {"base_url": "https://fleet.invalid", "secret": SECRET,
                    "key": {"id": state["id"], "slot_ids": state["slot_ids"], "mode": state["mode"],
                            "provider": state["provider"]}}
        if path == "/api/gateway/status":
            assert token == SECRET and method == "GET" and payload is None
            selected = parse_qs(urlsplit(url).query).get("slot", [])
            rows = [row for row in offered if row["id"] in state["slot_ids"]]
            if selected:
                assert len(selected) == 1 and selected[0] in state["slot_ids"]
                rows = [row for row in rows if row["id"] == selected[0]]
            return {"ready": ready_before if state["id"] == "gsynthetic" else True,
                    "provider": state["provider"], "mode": state["mode"],
                    "key": copy.deepcopy(state), "slots": [{**row, "ready": True} for row in rows]}
        if path == "/api/cli/gateway/revoke":
            # A rejected newly returned key may be revoked, never the existing one.
            assert payload["key_id"] == "gnew"
            return {"revoked": True}
        pytest.fail("unexpected network route")

    monkeypatch.setitem(client.scope, "api_json", api)
    return calls, state


def enrollments(calls):
    return [call for call in calls if urlsplit(call[0]).path == "/api/cli/gateway"]


def setup(client, *args):
    return client.loaded["main"](["gateway", "setup", *args])


def test_explicit_devin_setup_uses_one_fresh_owned_slot_and_validated_provider(discovery, monkeypatch, capsys):
    calls, _ = network(discovery, monkeypatch)
    assert setup(discovery, "--provider", "devin", "--slots", "reused") == 0
    assert enrollments(calls)[0][2]["provider"] == "devin"
    assert enrollments(calls)[0][2]["slot_ids"] == ["fresh-slot"]
    assert discovery.loaded["load_gateway"]()["provider"] == "devin"
    pairings_unchanged(discovery)
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err and not discovery.calls


def test_new_unspecified_provider_defaults_to_anthropic(discovery, monkeypatch):
    calls, _ = network(discovery, monkeypatch)
    assert setup(discovery, "--slots", "one") == 0
    assert enrollments(calls)[0][2].get("provider", "anthropic") == "anthropic"
    assert discovery.loaded["load_gateway"]().get("provider", "anthropic") == "anthropic"
    pairings_unchanged(discovery)


@pytest.mark.parametrize("case", ["multiple", "selected_unavailable", "globally_unavailable", "not_owned"])
def test_devin_unavailable_or_non_owned_selection_never_enrolls(discovery, monkeypatch, case):
    available = slots()
    options = None
    names = ["reused"]
    if case == "multiple":
        names = ["one", "reused"]
    elif case == "selected_unavailable":
        available[1]["providers"] = ["anthropic"]
    elif case == "globally_unavailable":
        options = ["anthropic"]
    else:
        names = ["someone-elses-slot"]
    calls, _ = network(discovery, monkeypatch, available_slots=available, options_providers=options)
    assert setup(discovery, "--provider", "devin", "--slots", *names) == 2
    assert not enrollments(calls) and discovery.loaded["load_gateway"]() is None
    assert not discovery.calls
    pairings_unchanged(discovery)


@pytest.mark.parametrize("provider", ["unknown", True, None, {"private": SECRET}])
def test_malformed_status_provider_never_silently_defaults_or_leaks(discovery, monkeypatch, capsys, provider):
    saved = profile(discovery, provider="devin", slot_ids=["slot-one"])
    before = (discovery.loaded["home"]() / "gateway.json").read_bytes()
    def api(*args, **kwargs):
        return {"ready": True, "key": {"id": saved["key_id"], "slot_ids": ["slot-one"],
                "mode": "sticky", "provider": provider}}
    monkeypatch.setitem(discovery.scope, "api_json", api)
    assert discovery.loaded["main"](["gateway", "status"]) == 2
    assert (discovery.loaded["home"]() / "gateway.json").read_bytes() == before
    pairings_unchanged(discovery)
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err


@pytest.mark.parametrize("provider", ["anthropic", "unknown", {"private": SECRET}])
def test_enrollment_cannot_accept_a_different_or_invalid_provider(discovery, monkeypatch, capsys, provider):
    calls, _ = network(discovery, monkeypatch, returned_provider=provider)
    assert setup(discovery, "--provider", "devin", "--slots", "reused") == 2
    assert len(enrollments(calls)) == 1 and discovery.loaded["load_gateway"]() is None
    pairings_unchanged(discovery)
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err


def test_unchanged_setup_without_provider_keeps_existing_devin_key(discovery, monkeypatch):
    saved = profile(discovery, provider="devin", slot_ids=["slot-one"])
    before = (discovery.loaded["home"]() / "gateway.json").read_bytes()
    calls, _ = network(discovery, monkeypatch, current_provider="devin")
    assert setup(discovery, "--slots", "one") == 0
    assert not enrollments(calls)
    assert discovery.loaded["load_gateway"]()["key_id"] == saved["key_id"]
    assert discovery.loaded["load_gateway"]()["provider"] == "devin"
    assert (discovery.loaded["home"]() / "gateway.json").read_bytes() == before
    pairings_unchanged(discovery)


@pytest.mark.parametrize("local_provider", ["devin", "anthropic"])
def test_reconfiguration_without_provider_keeps_current_website_provider(discovery, monkeypatch, local_provider):
    profile(discovery, provider=local_provider, slot_ids=["slot-one"])
    calls, _ = network(discovery, monkeypatch, current_provider="devin")
    assert setup(discovery, "--slots", "reused") == 0
    assert enrollments(calls)[0][2]["provider"] == "devin"
    assert discovery.loaded["load_gateway"]()["provider"] == "devin"
    assert discovery.loaded["load_gateway"]()["slot_ids"] == ["fresh-slot"]
    pairings_unchanged(discovery)


def test_status_refresh_preserves_web_choice_without_rewriting_saved_pairing_or_key(discovery, monkeypatch):
    saved = profile(discovery, provider="anthropic", slot_ids=["slot-one"])
    network(discovery, monkeypatch, current_provider="devin")
    discovery.loaded["check_gateway"](saved)
    assert saved["provider"] == "devin" and saved["secret"] == SECRET
    pairings_unchanged(discovery)


def test_explicit_devin_reuses_key_after_website_changed_cached_anthropic(discovery, monkeypatch):
    saved = profile(discovery, provider="anthropic", slot_ids=["slot-one"])
    calls, state = network(discovery, monkeypatch, current_provider="devin")
    assert setup(discovery, "--provider", "devin", "--slots", "one") == 0
    assert not enrollments(calls) and state["provider"] == "devin"
    assert discovery.loaded["load_gateway"]()["key_id"] == saved["key_id"]
    assert discovery.loaded["load_gateway"]()["secret"] == saved["secret"]
    pairings_unchanged(discovery)


def test_unready_devin_metadata_still_allows_explicit_switch_to_anthropic(discovery, monkeypatch):
    profile(discovery, provider="devin", slot_ids=["slot-one"])
    calls, _ = network(discovery, monkeypatch, current_provider="devin", ready_before=False)
    assert setup(discovery, "--provider", "anthropic", "--slots", "one") == 0
    assert enrollments(calls)[0][2]["provider"] == "anthropic"
    assert discovery.loaded["load_gateway"]()["provider"] == "anthropic"
    pairings_unchanged(discovery)


@pytest.mark.parametrize("invalid", [None, "not-a-list", "duplicates", "foreign-id", "object-name", "control-name"])
def test_malformed_status_slots_never_launch_or_overwrite_metadata(discovery, monkeypatch, invalid):
    saved = profile(discovery, provider="anthropic", slot_ids=["slot-one"])
    original = copy.deepcopy(saved)
    raw_before = (discovery.loaded["home"]() / "gateway.json").read_bytes()
    valid = {"id": "slot-one", "name": "one", "state": "active", "ready": True}
    entries = invalid
    if invalid == "duplicates":
        entries = [valid, copy.deepcopy(valid)]
    elif invalid == "foreign-id":
        entries = [{**valid, "id": "not-selected"}]
    elif invalid == "object-name":
        entries = [{**valid, "name": {"private": SECRET}}]
    elif invalid == "control-name":
        entries = [{**valid, "name": "\x1b[2J" + SECRET}]
    def api(*args, **kwargs):
        return {"ready": False, "provider": "devin", "key": {"id": saved["key_id"],
            "slot_ids": ["slot-one"], "mode": "sticky", "provider": "devin"}, "slots": entries}
    monkeypatch.setitem(discovery.scope, "api_json", api)
    with pytest.raises(discovery.loaded["CliError"]) as rejected:
        discovery.loaded["check_gateway"](saved, require_ready=False)
    assert saved == original and SECRET not in str(rejected.value)
    assert (discovery.loaded["home"]() / "gateway.json").read_bytes() == raw_before
    assert not discovery.calls and not discovery.retired_grants
    pairings_unchanged(discovery)


def test_explicit_provider_change_creates_requested_connection_without_revoking_previous(discovery, monkeypatch):
    profile(discovery, provider="devin", slot_ids=["slot-one"])
    calls, _ = network(discovery, monkeypatch, current_provider="devin")
    assert setup(discovery, "--provider", "anthropic", "--slots", "one") == 0
    assert enrollments(calls)[0][2]["provider"] == "anthropic"
    assert not any(urlsplit(call[0]).path.endswith("/revoke") for call in calls)
    assert discovery.loaded["load_gateway"]()["provider"] == "anthropic"
    pairings_unchanged(discovery)


def test_reused_cached_pair_name_pins_fresh_endpoint_slot_without_old_SSH_or_grants(discovery, monkeypatch):
    profile(discovery, provider="devin", slot_ids=["fresh-slot"])
    calls, _ = network(discovery, monkeypatch, current_provider="devin", current_ids=["fresh-slot"])
    assert invoke(discovery, "--slot", "reused", "--model", "opus", "--effort", "max") == 0
    assert urlsplit(calls[0][0]).path == "/api/gateway/status" and not urlsplit(calls[0][0]).query
    assert all("old-slot" not in call[0] for call in calls)
    assert parse_qs(urlsplit(calls[-1][0]).query) == {"slot": ["fresh-slot"]}
    assert discovery.calls[0][3]["env"]["ANTHROPIC_CUSTOM_HEADERS"] == "X-CCFleet-Slot: fresh-slot"
    assert "retired" not in discovery.retired_grants
    pairings_unchanged(discovery)


@pytest.mark.parametrize("case", ["unknown", "ambiguous", "not_selected", "duplicate_id"])
def test_https_slot_ambiguity_or_unselected_target_stops_without_launch(discovery, monkeypatch, case):
    chosen = ["fresh-slot"]
    offered = slots()
    wanted = "missing"
    if case == "ambiguous":
        chosen += ["another-slot"]
        offered.append({"id": "another-slot", "name": "reused", "state": "claimed", "providers": ["devin"]})
        wanted = "reused"
    elif case == "not_selected":
        wanted = "other"
    elif case == "duplicate_id":
        offered.append(copy.deepcopy(offered[1]))
        wanted = "reused"
    profile(discovery, provider="devin", slot_ids=chosen)
    calls, _ = network(discovery, monkeypatch, current_provider="devin", current_ids=chosen,
                       available_slots=offered)
    assert invoke(discovery, "--slot", wanted) == 2
    assert not discovery.calls and not discovery.retired_grants
    assert not any(parse_qs(urlsplit(call[0]).query).get("slot") == ["old-slot"] for call in calls)
    pairings_unchanged(discovery)


def test_default_list_is_only_saved_pairings_and_never_calls_network(discovery, monkeypatch, capsys):
    monkeypatch.setitem(discovery.scope, "api_json", lambda *args, **kwargs: pytest.fail("cached list made a request"))
    assert discovery.loaded["main"](["list"]) == 0
    output = capsys.readouterr().out
    assert "old-slot" in output and "reused" in output and "fresh-slot" not in output
    pairings_unchanged(discovery)


def test_remote_list_uses_device_auth_fresh_owned_options_without_rewriting_pairings(discovery, monkeypatch, capsys):
    offered = slots()
    offered[1]["account_fp"] = SECRET
    calls, _ = network(discovery, monkeypatch, available_slots=offered)
    assert discovery.loaded["main"](["list", "--remote"]) == 0
    assert len(calls) == 1 and calls[0][0].endswith("/api/cli/gateway/options")
    assert calls[0][1] == discovery.device["device_token"] and calls[0][3] == "GET"
    output = capsys.readouterr().out
    assert "fresh-slot" in output and "reused" in output and "old-slot" not in output
    assert SECRET not in output and discovery.device["device_token"] not in output
    assert discovery.loaded["load_gateway"]() is None
    pairings_unchanged(discovery)


def test_remote_list_without_pairing_does_not_discover_or_create_credentials(discovery, monkeypatch, capsys):
    discovery.loaded["save_config"]({"devices": {}, "active": ""})
    before = discovery.loaded["config_path"]().read_bytes()
    monkeypatch.setitem(discovery.scope, "api_json", lambda *args, **kwargs: pytest.fail("unauthorized discovery"))
    assert discovery.loaded["main"](["list", "--remote"]) == 2
    assert discovery.loaded["config_path"]().read_bytes() == before
    assert discovery.loaded["load_gateway"]() is None
    assert not discovery.calls
