"""Direct HTTPS native launches and endpoint keys use private, bounded local storage."""
from __future__ import annotations

import json
import os
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SECRET = "ccf_api_" + "x" * 43


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CCFLEET_HOME", str(tmp_path / "ccfleet"))
    loaded = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    scope = loaded["cmd_local"].__globals__
    device = {"device_id": "device", "device_token": "synthetic-device-token",
              "server": "https://fleet.invalid", "slot_id": "slot-one", "slot_name": "one"}
    loaded["save_config"]({"devices": {"device": device}, "active": "device"})
    monkeypatch.setitem(scope, "find_local_claude", lambda: "/synthetic/claude")
    monkeypatch.setitem(scope, "check_managed_route", lambda: None)
    monkeypatch.setitem(scope, "retire_live_grants", lambda *a, **kw: None)
    monkeypatch.setitem(scope, "inference_client", lambda: pytest.fail("SSH bridge helper loaded"))
    monkeypatch.setitem(scope, "check_inference", lambda *a, **kw: pytest.fail("laptop SSH opened"))
    calls = []
    def launch(command, **kwargs):
        path = Path(command[command.index("--settings") + 1])
        calls.append((command, kwargs, path, json.loads(path.read_text()), path.stat().st_mode & 0o777))
        return 0
    monkeypatch.setattr(loaded["subprocess"], "call", launch)
    return SimpleNamespace(loaded=loaded, scope=scope, device=device, calls=calls, project=tmp_path)


def profile(client, **changes):
    value = {"version": 1, "device_id": "device", "base_url": "https://fleet.invalid",
             "secret": SECRET, "key_id": "gsynthetic", "slot_ids": ["slot-one", "slot-two"],
             "mode": "sticky", **changes}
    client.loaded["save_gateway"](value)
    return value


def status(client, monkeypatch, **changes):
    calls = []
    def api(url, token, *a, **kw):
        calls.append((url, token, kw))
        return {"ready": True, "mode": "sticky", **changes}
    monkeypatch.setitem(client.scope, "api_json", api)
    return calls


def invoke(client, *flags):
    return client.loaded["main"](["local", "--project", str(client.project), *flags])


def test_gateway_launch_uses_remote_https_no_bridge_and_removes_secret_settings(client, monkeypatch, capsys):
    profile(client)
    rpc = status(client, monkeypatch)
    assert invoke(client) == 0
    command, kwargs, path, settings, mode = client.calls[0]
    assert rpc == [("https://fleet.invalid/api/gateway/status", SECRET, {"method": "GET"})]
    assert mode == 0o600 and not path.exists()
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "https://fleet.invalid"
    assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == SECRET
    assert settings["env"]["ANTHROPIC_CUSTOM_HEADERS"] == ""
    assert SECRET not in " ".join(command)
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert "HTTPS gateway" in captured.err


def test_explicit_slot_pins_header_without_cross_account_fallback(client, monkeypatch):
    profile(client)
    rpc = status(client, monkeypatch)
    assert invoke(client, "--slot", "one") == 0
    assert rpc[0][0] == "https://fleet.invalid/api/gateway/status"
    assert rpc[-1][0].endswith("?slot=slot-one")
    assert client.calls[0][3]["env"]["ANTHROPIC_CUSTOM_HEADERS"] == "X-CCFleet-Slot: slot-one"


def test_gateway_readiness_failure_starts_no_claude_no_bridge(client, monkeypatch, capsys):
    profile(client)
    status(client, monkeypatch, ready=False)
    assert invoke(client) == 2
    assert not client.calls
    assert "No model request or bridge fallback" in capsys.readouterr().err


def test_gateway_error_body_cannot_leak_key_to_terminal(client, monkeypatch, capsys):
    profile(client)
    def api(*a, **kw):
        raise client.loaded["CliError"]("server reflected " + SECRET, status=403)
    monkeypatch.setitem(client.scope, "api_json", api)
    assert invoke(client) == 2
    assert SECRET not in capsys.readouterr().err


def test_gateway_without_profile_requires_explicit_setup(client, capsys):
    assert invoke(client, "--transport", "gateway") == 2
    assert "ccfleet gateway setup" in capsys.readouterr().err
    assert not client.calls


def test_profile_is_separate_private_and_roundtrips(client):
    saved = profile(client)
    path = client.loaded["home"]() / "gateway.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert client.loaded["load_gateway"]() == saved
    assert SECRET not in client.loaded["config_path"]().read_text()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "public"])
def test_unsafe_gateway_file_rejected(client, kind, tmp_path):
    profile(client)
    path = client.loaded["home"]() / "gateway.json"
    if kind == "public":
        path.chmod(0o644)
    else:
        original = tmp_path / "original"
        path.rename(original)
        if kind == "symlink":
            path.symlink_to(original)
        elif kind == "hardlink":
            os.link(original, path)
        else:
            os.mkfifo(path, 0o600)
    with pytest.raises(client.loaded["CliError"]):
        client.loaded["load_gateway"]()


def test_gateway_save_rejects_symlink_directory_without_changing_target(client, tmp_path, monkeypatch):
    value = profile(client)
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("CCFLEET_HOME", str(alias))
    with pytest.raises(client.loaded["CliError"]):
        client.loaded["save_gateway"](value)
    assert target.stat().st_mode & 0o777 == 0o755 and not list(target.iterdir())


@pytest.mark.parametrize("url", ["http://evil.invalid", "https://user:secret@fleet.invalid",
                                 "https://fleet.invalid/path", "https://fleet.invalid?token=x",
                                 "https://fleet.invalid/#fragment", "https://fleet.invalid:bad"])
def test_gateway_url_refuses_credential_unsafe_destinations(client, url):
    with pytest.raises(client.loaded["CliError"]):
        profile(client, base_url=url)


def test_setup_selects_only_own_slots_in_user_order_and_never_prints_key(client, monkeypatch, capsys):
    calls = []
    def api(url, token, payload=None, method="POST", **kwargs):
        calls.append((url, token, payload, method))
        if url.endswith("/api/gateway/status"):
            return {"ready": True}
        if url.endswith("/options"):
            return {"available": True, "base_url": "https://fleet.invalid",
                    "slots": [{"id": "slot-one", "name": "one"}, {"id": "slot-two", "name": "two"}]}
        return {"base_url": "https://fleet.invalid", "secret": SECRET,
                "key": {"id": "gsynthetic", "slot_ids": payload["slot_ids"], "mode": payload["mode"]}}
    monkeypatch.setitem(client.scope, "api_json", api)
    assert client.loaded["main"](["gateway", "setup", "--slots", "two", "one", "--routing", "priority"]) == 0
    assert calls[1][2]["slot_ids"] == ["slot-two", "slot-one"]
    assert client.loaded["load_gateway"]()["mode"] == "priority"
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert "no local HTTP listener" in output.out


def test_bad_slot_choice_never_enrolls(client, monkeypatch):
    calls = []
    def api(url, token, *a, **kw):
        calls.append(url)
        return {"available": True, "base_url": "https://fleet.invalid",
                "slots": [{"id": "slot-one", "name": "one"}]}
    monkeypatch.setitem(client.scope, "api_json", api)
    assert client.loaded["main"](["gateway", "setup", "--slots", "somebody-elses-slot"]) == 2
    assert len(calls) == 1 and client.loaded["load_gateway"]() is None


def test_pin_an_owned_endpoint_slot_without_pairing_ssh_to_it(client, monkeypatch):
    profile(client)
    rpc = status(client, monkeypatch, slots=[{"id": "slot-two", "name": "two", "ready": True},
                                            {"id": "slot-one", "name": "one", "ready": False}])
    assert invoke(client, "--slot", "two") == 0
    assert rpc[-1][0].endswith("?slot=slot-two")
    assert client.calls[0][3]["env"]["ANTHROPIC_CUSTOM_HEADERS"] == "X-CCFleet-Slot: slot-two"


def test_endpoint_metadata_refreshed_after_web_routing_change(client, monkeypatch):
    profile(client, slot_ids=["slot-two"], mode="sticky")
    status(client, monkeypatch, key={"id": "gsynthetic", "slot_ids": ["slot-one"], "mode": "priority"})
    assert invoke(client, "--slot", "one") == 0
    assert client.calls[0][3]["env"]["ANTHROPIC_CUSTOM_HEADERS"] == "X-CCFleet-Slot: slot-one"


def test_gateway_doctor_uses_healthy_backup_without_ssh_or_original_slot_failure(client, monkeypatch):
    from ccfleet_agent import client_experience
    profile(client)
    status(client, monkeypatch, slots=[{"id": "slot-one", "ready": False},
                                     {"id": "slot-two", "ready": True}])
    callbacks = client.loaded["experience_callbacks"]()
    callbacks["which"] = lambda _name: pytest.fail("SSH dependencies checked for gateway")
    callbacks["run_version"] = lambda _binary, **kwargs: "2.1.0"
    callbacks["device_context"] = lambda _device: pytest.fail("original slot checked")
    callbacks["check_inference"] = lambda *a, **kw: pytest.fail("SSH relay checked")
    report = client_experience.safe_report(client_experience.doctor(callbacks, privacy=True))
    assert report["ok"] is True and report["transport"] == "gateway"
    assert report["gateway"] == {"transport": "gateway", "ready": True, "routing": "sticky",
                                 "selected_count": 2, "eligible_count": 1}
    text = json.dumps(report)
    assert SECRET not in text and "slot-one" not in text and "slot-two" not in text
    assert "Our gateway can read" in text and "inner SSH" not in text


def test_gateway_failure_diagnostic_never_falls_back_or_exports_raw_error(client, monkeypatch):
    from ccfleet_agent import client_experience
    profile(client)
    def api(*a, **kw):
        raise RuntimeError("reflected " + SECRET)
    monkeypatch.setitem(client.scope, "api_json", api)
    callbacks = client.loaded["experience_callbacks"]()
    callbacks["device_context"] = lambda _device: pytest.fail("fallback device checked")
    report = client_experience.safe_report(client_experience.status(callbacks))
    assert report["ok"] is False and report["transport"] == "gateway"
    assert SECRET not in json.dumps(report)
    assert [check["code"] for check in report["checks"]] == ["gateway_unavailable"]


def retry_clock(client, monkeypatch):
    clock = {"now": 0.0, "sleeps": []}
    monkeypatch.setattr(client.loaded["time"], "monotonic", lambda: clock["now"])
    def sleep(seconds):
        clock["sleeps"].append(seconds)
        clock["now"] += seconds
    monkeypatch.setattr(client.loaded["time"], "sleep", sleep)
    return clock


def test_fresh_gateway_setup_enrolls_once_then_waits_read_only_for_service_key(client, monkeypatch, capsys):
    clock = retry_clock(client, monkeypatch)
    calls, probes = [], []
    def api(url, token, payload=None, method="POST", **kwargs):
        calls.append((url, method, payload))
        if url.endswith("/options"):
            return {"available": True, "base_url": "https://fleet.invalid",
                    "slots": [{"id": "slot-one", "name": "one"}]}
        if url.endswith("/api/gateway/status"):
            probes.append(kwargs["timeout"])
            return {"ready": len(probes) >= 3}
        assert url.endswith("/api/cli/gateway") and method == "POST"
        return {"base_url": "https://fleet.invalid", "secret": SECRET,
                "key": {"id": "gsynthetic", "slot_ids": ["slot-one"], "mode": "sticky"}}
    monkeypatch.setitem(client.scope, "api_json", api)
    assert client.loaded["main"](["gateway", "setup"]) == 0
    assert len([call for call in calls if call[1] == "POST"]) == 1
    assert probes == [20, 20, 20] and clock["sleeps"] == [5, 5]
    output = capsys.readouterr()
    assert output.err.count("Waiting for selected slots") == 1
    assert SECRET not in output.out + output.err
    assert client.loaded["load_gateway"]()["secret"] == SECRET


def test_gateway_setup_deadline_keeps_key_without_reenrolling_or_fallback(client, monkeypatch, capsys):
    saved = profile(client)
    clock = retry_clock(client, monkeypatch)
    monkeypatch.setitem(client.scope, "SETUP_WAIT_SECONDS", 32)
    budgets = []
    def api(url, token, *args, **kwargs):
        assert kwargs["method"] == "GET" and url.endswith("/api/gateway/status")
        budgets.append(kwargs["timeout"])
        clock["now"] += kwargs["timeout"]
        raise client.loaded["CliError"]("synthetic connection failure")
    monkeypatch.setitem(client.scope, "api_json", api)
    with pytest.raises(client.loaded["CliError"], match="configuration is saved"):
        client.loaded["wait_for_gateway_setup"](saved)
    assert budgets == [20, 7] and clock["now"] == 32
    assert client.loaded["load_gateway"]()["key_id"] == "gsynthetic"
    assert SECRET not in capsys.readouterr().err


def test_gateway_setup_denied_key_is_never_retried_or_removed(client, monkeypatch):
    saved = profile(client)
    clock = retry_clock(client, monkeypatch)
    calls = []
    def api(*args, **kwargs):
        calls.append(kwargs)
        raise client.loaded["CliError"]("reflected " + SECRET, status=401)
    monkeypatch.setitem(client.scope, "api_json", api)
    with pytest.raises(client.loaded["CliError"], match="check was denied") as captured:
        client.loaded["wait_for_gateway_setup"](saved)
    assert len(calls) == 1 and clock["sleeps"] == []
    assert SECRET not in str(captured.value)
    assert client.loaded["load_gateway"]()["key_id"] == "gsynthetic"


def test_changed_gateway_setup_keeps_previous_key_for_running_launches(client, monkeypatch, capsys):
    previous = profile(client, slot_ids=["slot-one"])
    calls = []
    replacement = "ccf_api_" + "y" * 43
    def api(url, token, payload=None, method="POST", **kwargs):
        calls.append((url, token, payload, method))
        if url.endswith("/options"):
            return {"available": True, "base_url": "https://fleet.invalid",
                    "slots": [{"id": "slot-one", "name": "one"}, {"id": "slot-two", "name": "two"}]}
        if url.endswith("/api/gateway/status"):
            assert token in {previous["secret"], replacement}
            return {"ready": True}
        assert url.endswith("/api/cli/gateway")
        return {"base_url": "https://fleet.invalid", "secret": replacement,
                "key": {"id": "gnew", "slot_ids": payload["slot_ids"], "mode": payload["mode"]}}
    monkeypatch.setitem(client.scope, "api_json", api)
    assert client.loaded["main"](["gateway", "setup", "--slots", "one", "two"]) == 0
    assert not any(url.endswith("/revoke") for url, *_ in calls)
    assert client.loaded["load_gateway"]()["key_id"] == "gnew"
    output = capsys.readouterr()
    assert "previous endpoint key was kept so existing launches can finish" in output.err
    assert previous["secret"] not in output.out + output.err and replacement not in output.out + output.err


def test_paired_explicit_gateway_slot_keeps_its_legacy_history_and_cleanup_scope(client, monkeypatch):
    profile(client)
    second = {**client.device, "device_id": "second", "slot_id": "slot-two", "slot_name": "two"}
    config = client.loaded["load_config"]()
    config["devices"]["second"] = second
    client.loaded["save_config"](config)
    legacy = client.loaded["home"]() / "local" / client.loaded["hashlib"].sha256(b"slot-two").hexdigest()[:16]
    legacy.mkdir(mode=0o700, parents=True)
    status(client, monkeypatch)
    cleanup = []
    monkeypatch.setitem(client.scope, "retire_live_grants", lambda selected, **kw: cleanup.append(selected["device_id"]))
    assert invoke(client, "--slot", "two", "--legacy-history") == 0
    assert cleanup == ["second"]
    assert client.calls[0][1]["env"]["CLAUDE_CONFIG_DIR"] == str(legacy)
    assert client.calls[0][3]["env"]["ANTHROPIC_CUSTOM_HEADERS"] == "X-CCFleet-Slot: slot-two"


def test_unpaired_explicit_gateway_slot_uses_its_own_legacy_preview_history(client, monkeypatch):
    profile(client)
    legacy = client.loaded["home"]() / "local" / client.loaded["hashlib"].sha256(b"slot-two").hexdigest()[:16]
    legacy.mkdir(mode=0o700, parents=True)
    status(client, monkeypatch, slots=[{"id": "slot-two", "name": "two", "ready": True}])
    assert invoke(client, "--slot", "two", "--legacy-history") == 0
    assert client.calls[0][1]["env"]["CLAUDE_CONFIG_DIR"] == str(legacy)


def test_fixed_gateway_slot_failure_never_uses_a_ready_backup(client, monkeypatch):
    profile(client)
    calls = []
    def api(url, token, *a, **kw):
        calls.append(url)
        if url.endswith("/api/gateway/status"):
            return {"ready": True, "slots": [{"id": "slot-one", "name": "one", "ready": False},
                                             {"id": "slot-two", "name": "two", "ready": True}]}
        assert url.endswith("?slot=slot-one")
        return {"ready": False, "slots": [{"id": "slot-two", "name": "two", "ready": True}]}
    monkeypatch.setitem(client.scope, "api_json", api)
    assert invoke(client, "--slot", "one") == 2
    assert calls == ["https://fleet.invalid/api/gateway/status",
                     "https://fleet.invalid/api/gateway/status?slot=slot-one"]
    assert not client.calls


def test_gateway_launch_describes_fixed_account_and_pool_mode(client, monkeypatch, capsys):
    profile(client, slot_ids=["slot-one"])
    status(client, monkeypatch)
    assert invoke(client) == 0
    assert "one fixed selected account; automatic switching is disabled" in capsys.readouterr().err
    profile(client, mode="priority")
    assert invoke(client) == 0
    assert "priority order for each request" in capsys.readouterr().err
