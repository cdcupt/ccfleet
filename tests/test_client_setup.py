"""One setup path: reuse pairing, hidden input, bounded readiness, no project side effects."""

from __future__ import annotations

import os
import pty
import runpy
import select
import signal
import sys
import time
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CCFLEET_HOME", str(tmp_path / "client"))
    result = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    monkeypatch.setattr(result["shutil"], "which", lambda name: "/usr/bin/" + name)
    # A verified helper is loaded, but setup must never scan or apply a project.
    def forbidden(*a, **kw):
        pytest.fail("setup attempted project file access")
    monkeypatch.setitem(result["cmd_setup"].__globals__, "project_files",
                        lambda: SimpleNamespace(snapshot=forbidden, apply_snapshot=forbidden))
    monkeypatch.setitem(result["cmd_setup"].__globals__, "live_files", lambda: SimpleNamespace())
    monkeypatch.setitem(result["cmd_setup"].__globals__, "live_client", lambda: SimpleNamespace())
    monkeypatch.setitem(result["cmd_setup"].__globals__, "live_control",
                        lambda *a, **kw: {"ready": True})
    from ccfleet_agent import inference_client
    helper = SimpleNamespace(RelayError=inference_client.RelayError,
                             check_status=lambda *a, **kw: {"ready": True, "protocol": 2})
    monkeypatch.setitem(result["cmd_setup"].__globals__, "inference_client", lambda: helper)
    result["_inference_fixture"] = helper
    return result


def device(client, name="one"):
    client["ensure_home"]()
    key, pin = client["home"]() / (name + ".key"), client["home"]() / (name + ".hosts")
    key.write_text("FAKE_PRIVATE_KEY")
    pin.write_text("FAKE_PUBLIC_PIN")
    return {"device_id": name, "device_token": "FAKE_TOKEN_" + name,
            "endpoint": "wss://fleet.invalid/connect", "slot_id": "slot-" + name,
            "slot_name": "slot-" + name, "user": "slot01", "key": str(key),
            "known_hosts": str(pin), "host_alias": "ccfleet-" + name,
            "server": "https://fleet.invalid"}


def save(client, *devices, active=None):
    client["save_config"]({"version": 1, "devices": {d["device_id"]: d for d in devices},
                           "active": active or (devices[0]["device_id"] if devices else ""),
                           "projects": {"saved": {"id": "private-project-history"}}})


def forbid_pairing(client, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("existing computer was re-paired")
    monkeypatch.setitem(client["cmd_setup"].__globals__, "cmd_login", forbidden)


def test_existing_setup_preserves_keys_config_history_and_device_label(client, monkeypatch, capsys):
    saved = device(client)
    save(client, saved)
    before = client["config_path"]().read_bytes()
    forbid_pairing(client, monkeypatch)
    calls = []
    def rpc(selected, **kw):
        calls.append((selected, "status", kw))
        return {"ready": True, "protocol": 2}
    monkeypatch.setitem(client["cmd_setup"].__globals__, "check_inference", rpc)
    assert client["main"](["setup", "--name", "new-label-must-not-repair"]) == 0
    assert client["config_path"]().read_bytes() == before
    assert Path(saved["key"]).read_text() == "FAKE_PRIVATE_KEY"
    assert len(calls) == 1 and calls[0][1] == "status"
    assert set(calls[0][2]) == {"timeout"} and 0 < calls[0][2]["timeout"] <= 25
    output = capsys.readouterr()
    assert "Already paired" in output.out and "No project was uploaded" in output.out
    assert saved["device_token"] not in output.out + output.err


def test_new_setup_registers_once_then_waits_for_key_propagation(client, monkeypatch, capsys):
    logins, probes, sleeps = [], [], []
    clock = {"now": 0.0}
    def login(args):
        logins.append(args)
        save(client, device(client))
        return 0
    def rpc(selected, **kw):
        probes.append((selected["device_id"], "status", kw["timeout"]))
        if len(probes) < 3:
            raise client["CliError"]("key has not converged", code="connection")
        return {"ready": True, "protocol": 2}
    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds
    scope = client["cmd_setup"].__globals__
    monkeypatch.setitem(scope, "cmd_login", login)
    monkeypatch.setitem(scope, "check_inference", rpc)
    monkeypatch.setattr(client["time"], "monotonic", lambda: clock["now"])
    monkeypatch.setattr(client["time"], "sleep", sleep)
    assert client["main"](["setup", "--name", "computer"]) == 0
    assert len(logins) == 1 and logins[0].code == "" and logins[0].name == "computer"
    assert [p[:2] for p in probes] == [("one", "status")] * 3
    assert sleeps == [5, 5]
    assert len(client["load_config"]()["devices"]) == 1
    assert "Waiting for the slot" in capsys.readouterr().err


def test_setup_deadline_keeps_successful_pairing_without_registering_again(client, monkeypatch, capsys):
    clock = {"now": 0.0}
    logins, budgets = [], []
    def login(args):
        logins.append(args)
        save(client, device(client))
    def rpc(_selected, **kw):
        budgets.append(kw["timeout"])
        clock["now"] += kw["timeout"]
        raise client["CliError"]("transport timeout", code="connection")
    scope = client["cmd_setup"].__globals__
    monkeypatch.setitem(scope, "cmd_login", login)
    monkeypatch.setitem(scope, "check_inference", rpc)
    monkeypatch.setitem(scope, "SETUP_WAIT_SECONDS", 32)
    monkeypatch.setattr(client["time"], "monotonic", lambda: clock["now"])
    monkeypatch.setattr(client["time"], "sleep",
                        lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    assert client["main"](["setup"]) == 2
    assert len(logins) == 1 and budgets == [25, 2] and clock["now"] == 32
    assert client["load_config"]()["active"] == "one"
    assert "paired, but slot readiness is not yet confirmed" in capsys.readouterr().err


@pytest.mark.parametrize("code", ["disabled", "account_unavailable", "invalid", "internal"])
def test_permanent_readiness_failures_stop_without_retry_or_cleanup(client, monkeypatch, capsys, code):
    calls = []
    def login(args):
        save(client, device(client))
    def rpc(*a, **kw):
        calls.append(kw)
        raise client["CliError"]("operator action required", code=code)
    scope = client["cmd_setup"].__globals__
    monkeypatch.setitem(scope, "cmd_login", login)
    monkeypatch.setitem(scope, "check_inference", rpc)
    assert client["main"](["setup"]) == 2
    assert len(calls) == 1 and client["load_config"]()["active"] == "one"
    assert "old setup has not been removed" in capsys.readouterr().err


def test_existing_connection_failure_never_repairs_or_changes_configuration(client, monkeypatch):
    save(client, device(client))
    original = client["config_path"]().read_bytes()
    forbid_pairing(client, monkeypatch)
    calls = []
    def rpc(*a, **kw):
        calls.append(kw)
        raise client["CliError"]("check network or revocation", code="connection")
    monkeypatch.setitem(client["cmd_setup"].__globals__, "check_inference", rpc)
    assert client["main"](["setup"]) == 2
    assert len(calls) == 1 and client["config_path"]().read_bytes() == original


@pytest.mark.parametrize("answer", [{"ready": 1, "protocol": 1}, {"ready": True, "protocol": True},
                                    {"ready": True, "protocol": "1"}, {"protocol": 1},
                                    {"ready": False, "protocol": 1}, {"ready": True, "protocol": 1}])
def test_setup_requires_strict_ready_and_integer_protocol(client, monkeypatch, answer):
    save(client, device(client))
    monkeypatch.setattr(client["_inference_fixture"], "check_status", lambda *a, **kw: answer)
    assert client["main"](["setup"]) == 2


def test_explicit_paired_slot_becomes_default_only_after_readiness(client, monkeypatch):
    one, two = device(client, "one"), device(client, "two")
    save(client, one, two)
    forbid_pairing(client, monkeypatch)
    def rpc(selected, *a, **kw):
        assert selected["device_id"] == "two"
        assert client["load_config"]()["active"] == "one"
        return {"ready": True, "protocol": 2}
    monkeypatch.setitem(client["cmd_setup"].__globals__, "check_inference", rpc)
    assert client["main"](["setup", "--slot", "slot-two"]) == 0
    config = client["load_config"]()
    assert config["active"] == "two" and len(config["devices"]) == 2
    assert config["projects"]["saved"]["id"] == "private-project-history"


@pytest.mark.parametrize("bad", ["missing_key", "missing_pin", "incomplete", "unknown_slot"])
def test_invalid_existing_pairing_is_not_replaced(client, monkeypatch, bad):
    saved = device(client)
    if bad == "missing_key":
        Path(saved["key"]).unlink()
    elif bad == "missing_pin":
        Path(saved["known_hosts"]).unlink()
    elif bad == "incomplete":
        saved.pop("device_token")
    save(client, saved)
    before = client["config_path"]().read_bytes()
    forbid_pairing(client, monkeypatch)
    def rpc(*a, **kw):
        pytest.fail("invalid pairing contacted the network")
    monkeypatch.setitem(client["cmd_setup"].__globals__, "check_inference", rpc)
    flags = ["--slot", "unknown"] if bad == "unknown_slot" else []
    assert client["main"](["setup", *flags]) == 2
    assert client["config_path"]().read_bytes() == before


def test_new_setup_slot_selection_does_not_consume_pairing_code(client, monkeypatch):
    forbid_pairing(client, monkeypatch)
    assert client["main"](["setup", "--slot", "slot-one"]) == 2
    assert not client["config_path"]().exists()


def test_uncertain_registration_is_never_retried(client, monkeypatch):
    attempts = []
    def login(args):
        attempts.append(args)
        raise client["CliError"]("registration response lost")
    monkeypatch.setitem(client["cmd_setup"].__globals__, "cmd_login", login)
    assert client["main"](["setup"]) == 2 and len(attempts) == 1


def test_setup_interrupt_before_pairing_preserves_empty_state(client, monkeypatch):
    def prompt():
        raise KeyboardInterrupt()
    monkeypatch.setitem(client["cmd_login"].__globals__, "prompt_pairing_code", prompt)
    assert client["main"](["setup"]) == 130
    assert not client["config_path"]().exists()


def test_pairing_without_controlling_terminal_never_reads_stdin(client, monkeypatch):
    def missing(*a, **kw):
        raise OSError("no controlling terminal")
    def forbidden(*a, **kw):
        pytest.fail("getpass fell back to installer stdin")
    monkeypatch.setattr(client["os"], "open", missing)
    monkeypatch.setattr(client["getpass"], "getpass", forbidden)
    with pytest.raises(client["CliError"], match="interactive terminal"):
        client["prompt_pairing_code"]()


def test_getpass_warning_cannot_enable_echoed_fallback(client, monkeypatch):
    monkeypatch.setattr(client["os"], "open", lambda *a, **kw: 99)
    monkeypatch.setattr(client["os"], "isatty", lambda fd: True)
    monkeypatch.setattr(client["os"], "close", lambda fd: None)
    def fallback(*a, **kw):
        warnings.warn("no echo control", client["getpass"].GetPassWarning, stacklevel=2)
        pytest.fail("fallback consumed input")
    monkeypatch.setattr(client["getpass"], "getpass", fallback)
    with pytest.raises(client["CliError"], match="interactive terminal"):
        client["prompt_pairing_code"]()


def test_hidden_pairing_uses_real_tty_even_when_stdin_is_a_script_pipe():
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"SCRIPT_STDIN_UNTOUCHED\n")
    os.close(write_fd)
    fake_code = "ccf_pair_PTY_FAKE_ONLY_718a19"
    program = ("import runpy,sys; c=runpy.run_path(" + repr(str(ROOT / "laptop/ccfleet")) + "); "
               "value=c['prompt_pairing_code'](); "
               "print('HIDDEN_PAIRING_OK' if value==" + repr(fake_code) + " else 'BAD_INPUT'); "
               "print(sys.stdin.readline().strip())")
    pid, fd = pty.fork()
    if pid == 0:
        os.dup2(read_fd, 0)
        os.close(read_fd)
        os.execv(sys.executable, [sys.executable, "-c", program])
    os.close(read_fd)
    output = bytearray()
    sent, finished = False, False
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    chunk = os.read(fd, 8192)
                except OSError:
                    chunk = b""
                output.extend(chunk)
                if not sent and b"Pairing code from your slot page:" in output:
                    os.write(fd, fake_code.encode() + b"\n")
                    sent = True
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                finished = True
                assert os.waitstatus_to_exitcode(status) == 0
                while select.select([fd], [], [], 0)[0]:
                    try:
                        chunk = os.read(fd, 8192)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                break
        assert finished and b"HIDDEN_PAIRING_OK" in output
        assert b"SCRIPT_STDIN_UNTOUCHED" in output
        assert fake_code.encode() not in output, "pairing code was echoed"
    finally:
        if not finished:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(fd)
