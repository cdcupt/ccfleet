"""The user-facing tool projects only timing, fixed codes, and public aggregate fields."""

import io
import json
import runpy
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ccfleet_agent import client_experience as diag

SOURCE = Path(__file__).resolve().parents[1] / "laptop/ccfleet"
PRIVATE = "private@example.invalid/a/private/path"


def doctor(args):
    cli = runpy.run_path(str(SOURCE))
    cli["cmd_diagnostics"].__globals__["client_experience"] = lambda: diag
    return cli["main"](["doctor", "--connection", *args])


def test_public_probe_verifies_health_body_and_preserves_timeout_milestones(monkeypatch):
    results = iter([(0, b'{"ok":true}\n200 .001 .010 .040 .092 .093'),
                    (28, b'\n000 .001 .010 0 0 8'),
                    (0, b'{"why":"private"}\n200 1 2 3 4 5'),
                    (0, b'\n302 .001 .010 .011 .012 .013')])

    def fake(command, timeout):
        assert Path(command[0]).name == "curl" and command[1] == "-q" and command[-1] == "https://ccfleet.io/healthz"
        assert "--location" not in command and "--insecure" not in command
        return next(results)

    monkeypatch.setattr(diag, "run_probe", fake)
    assert diag.public_probe()["first_byte_ms"] == 92
    timeout = diag.public_probe()
    assert timeout["state"] == "timeout" and timeout["tcp_connected_ms"] == 10
    assert timeout["tls_ready_ms"] is None and "http_status" not in timeout
    assert diag.public_probe()["state"] == "unexpected_response"
    assert diag.public_probe()["state"] == "http_error"


@pytest.mark.parametrize(("gateway", "codes", "ok", "expected"), [
    (True, ["gateway_ready"], True, "ready"),
    (True, ["gateway_unavailable"], False, "failed"),
    (False, ["device_ready", "relay_ready"], True, "ready"),
    (False, ["device_connection", "relay_ready"], False, "failed"),
    (False, ["device_ready", "relay_unchecked"], True, "unverified"),
    (False, ["pairing_missing", "relay_unchecked"], False, "failed"),
    (False, [PRIVATE], True, "unsupported_status"),
])
def test_selected_route_uses_shipped_client_and_filters_status(monkeypatch, gateway, codes, ok,
                                                              expected):
    def fake(command, timeout):
        assert command == [sys.executable, "-I", "ccfleet", "status", "--json", "--slot", "slot-1234"]
        assert timeout <= 45
        response = {"schema": 1, "kind": "status", "ok": ok,
                    "checks": [{"code": c, "message": PRIVATE} for c in codes], "token": PRIVATE}
        if gateway:
            response["transport"] = "gateway"
        return int(not ok), json.dumps(response).encode()
    monkeypatch.setattr(diag, "run_probe", fake)
    report = diag.route_probe("ccfleet", "slot-1234")
    assert report["state"] == expected
    assert PRIVATE not in json.dumps(report)


def test_client_timeout_and_missing_cli_are_distinct(monkeypatch):
    def timeout(*args):
        raise subprocess.TimeoutExpired("private", 45)
    monkeypatch.setattr(diag, "run_probe", timeout)
    assert diag.route_probe(None, None)["state"] == "cli_missing"
    assert diag.route_probe("ccfleet", None)["state"] == "status_timeout"


def test_named_pairing_history_uses_real_mcp_protocol(monkeypatch):
    counts = dict.fromkeys(("requests", *diag.MCP_OUTCOMES), 0)
    counts.update(requests=5, success=4, rate_limits=1)
    view = {"source": "cached_relay_aggregates", "available": True, "freshness": "stale",
            "window_kind": "utc_calendar_days", "window_days": 7, "age_seconds": 1234,
            "counts": counts, "latencies": {"first_byte_ms": {"samples": 4,
            "mean_ms": 1800, "max_ms": 3100}}, "token_usage": PRIVATE}
    # Serve through the shipping stdio implementation, including the real handshake.
    monkeypatch.setitem(diag._MCP_TOOLS, "ccfleet_relay_usage", (lambda status: view, ""))

    def fake(args, timeout, data):
        assert args == [sys.executable, "-I", "ccfleet", "mcp", "serve", "--slot", "slot-1234"]
        output = io.BytesIO()
        rc = diag.serve_mcp(lambda: {}, input_stream=io.BytesIO(data),
                                     output_stream=output)
        return rc, output.getvalue()
    monkeypatch.setattr(diag, "run_probe", fake)
    report = diag.slot_history("ccfleet", "slot-1234")
    assert report["counts"]["rate_limits"] == 1 and report["freshness"] == "stale"
    assert report["first_byte"]["samples"] == 4 and PRIVATE not in json.dumps(report)
    counts["success"] = 99
    assert diag.slot_history("ccfleet", "slot-1234") == {"available": False}


def test_intermittent_run_keeps_failures_then_recovery_and_omits_alias(monkeypatch, tmp_path):
    routes = iter([{"state": "failed", "checks": ["gateway_unavailable"], "transport": "gateway"},
                   {"state": "ready", "checks": ["gateway_ready"], "transport": "gateway"}])
    monkeypatch.setattr(diag, "public_probe", lambda: {"state": "ready", "first_byte_ms": 12})
    monkeypatch.setattr(diag, "route_probe", lambda client, slot: next(routes))
    monkeypatch.setattr(diag, "slot_history", lambda *a: pytest.fail("unverified pairing"))
    monkeypatch.setattr(diag.time, "sleep", lambda seconds: None)
    target = tmp_path / "report.json"
    args = ["--slot", PRIVATE, "--samples", "2", "--export", str(target)]
    assert doctor(args) == 1
    report = json.loads(target.read_text())
    assert report["summary"]["configured_route"]["states"] == {"failed": 1, "ready": 1}
    assert report["summary"]["public_https"]["completed"] == 2
    assert report["samples"][1]["configured_route"]["at_utc"].endswith("Z")
    assert report["named_pairing_history"]["available"] is False
    assert PRIVATE not in target.read_text()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    before = target.read_bytes()
    assert doctor(args) == 2
    assert target.read_bytes() == before


def test_interrupt_preserves_completed_work(monkeypatch, tmp_path):
    def stop(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(diag, "public_probe", lambda: {"state": "ready"})
    monkeypatch.setattr(diag, "route_probe", stop)
    path = tmp_path / "partial"
    assert doctor(["--export", str(path)]) == 130
    saved = json.loads(path.read_text())
    assert saved["interrupted"] and saved["summary"]["public_https"]["completed"] == 1
    assert saved["summary"]["configured_route"]["completed"] == 0


def test_export_rejects_concurrent_existing_file_and_symlink(tmp_path):
    path = tmp_path / "existing"
    path.write_text("keep")
    link = tmp_path / "link"
    link.symlink_to(path)
    for dest in (link, path):
        with pytest.raises(FileExistsError):
            diag.write_report({"new": True}, dest)
    assert path.read_text() == "keep" and list(sorted(tmp_path.iterdir())) == [path, link]


def test_real_cli_round_trip_gateway_recovers(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    client = SOURCE
    monkeypatch.setenv("CCFLEET_HOME", str(tmp_path / "client"))
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    states = iter([False, True])
    urls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            urls.append(self.path)
            ready = next(states)
            payload = json.dumps({"ready": ready, "mode": "sticky", "slots": [
                {"id": "fixture", "ready": ready}]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        origin = f"http://127.0.0.1:{server.server_port}"
        cli = runpy.run_path(str(client))
        cli["save_config"]({"devices": {"test": {"slot_id": "fixture", "device_id": "test",
                                                 "server": origin}}, "active": "test"})
        cli["save_gateway"]({"version": 1, "device_id": "test", "base_url": origin,
                             "key_id": "test", "mode": "sticky", "slot_ids": ["fixture"],
                             "secret": "ccf_api_" + "t" * 43})
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            first = diag.route_probe(str(client), None)
            second = diag.route_probe(str(client), None)
        finally:
            server.shutdown()
            worker.join()
    assert urls == ["/api/gateway/status", "/api/gateway/status"]
    assert first["state"] == "failed" and first["gateway"]["eligible"] == 0
    assert second == {"state": "ready", "transport": "gateway", "checks": ["gateway_ready"],
                      "gateway": {"routing": "sticky", "selected": 1, "eligible": 1}}


def test_doctor_json_is_parseable_and_runs_own_client(monkeypatch, tmp_path, capsys):
    def configured_route(client, slot):
        assert client == str(SOURCE) and slot == "slot-1234"
        return {"state": "ready", "transport": "gateway", "checks": ["gateway_ready"]}
    monkeypatch.setattr(diag, "route_probe", configured_route)
    monkeypatch.setattr(diag, "public_probe", lambda: {"state": "ready"})
    path = tmp_path / "saved.json"
    assert doctor(["--slot", "slot-1234", "--samples", "1", "--json", "--privacy",
                   "--export", str(path)]) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report == json.loads(path.read_text())
    assert report["slot"] == "slot-1234" and report["privacy"]
    assert "1/1" in output.err


def test_plain_doctor_does_not_silently_ignore_connection_options(capsys):
    assert runpy.run_path(str(SOURCE))["main"](["doctor", "--samples", "1"]) == 2
    assert "require doctor --connection" in capsys.readouterr().err
