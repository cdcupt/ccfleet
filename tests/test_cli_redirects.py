"""Authenticated API requests never carry their secret through HTTP redirects."""

from __future__ import annotations

import contextlib
import http.server
import json
import runpy
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "ccf_pair_fake_redirect_test_only_not_a_real_credential"


@pytest.fixture
def client(monkeypatch):
    # Keep these real HTTP tests entirely on loopback even in a proxied shell.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    return runpy.run_path(str(ROOT / "laptop/ccfleet"))


@contextlib.contextmanager
def http_endpoint():
    received = []
    routes = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            # BaseHTTPRequestHandler otherwise writes request paths to stderr.
            pass

        def handle_request(self):
            size = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(size) if size else b""
            received.append({"path": self.path, "method": self.command,
                             "authorization": self.headers.get("Authorization"), "body": body})
            status, destination = routes.get(self.path, (200, None))
            payload = json.dumps({"ok": True} if status == 200 else {"error": "redirect refused"})
            encoded = payload.encode()
            self.send_response(status)
            if destination is not None:
                self.send_header("Location", destination)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = handle_request
        do_POST = handle_request

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", routes, received
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_cross_origin_redirect_never_receives_pairing_secret(client, status, capsys, caplog):
    with http_endpoint() as (destination, _, received_at_destination):
        with http_endpoint() as (origin, routes, received_at_origin):
            routes["/api/cli/register"] = (status, destination + "/collect")
            with pytest.raises(client["CliError"]) as error:
                client["api_json"](origin + "/api/cli/register", TOKEN,
                                   {"public_key": "test-only-public-key", "device_name": "test"})
            assert error.value.status == status
            assert len(received_at_origin) == 1
            assert received_at_origin[0]["authorization"] == "Bearer " + TOKEN
            assert received_at_destination == []
            assert TOKEN not in str(error.value)
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err + caplog.text


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_same_origin_redirect_is_also_refused_instead_of_replaying_auth(client, status):
    with http_endpoint() as (origin, routes, received):
        routes["/api/cli/register"] = (status, origin + "/another-endpoint")
        with pytest.raises(client["CliError"]) as error:
            client["api_json"](origin + "/api/cli/register", TOKEN, {})
        assert error.value.status == status
        assert [request["path"] for request in received] == ["/api/cli/register"]
        assert TOKEN not in str(error.value)


def test_direct_authenticated_json_request_still_succeeds(client, capsys, caplog):
    with http_endpoint() as (origin, _, received):
        payload = {"public_key": "test-only-public-key", "device_name": "test"}
        assert client["api_json"](origin + "/api/cli/register", TOKEN, payload) == {"ok": True}
        assert len(received) == 1
        assert received[0]["method"] == "POST"
        assert received[0]["authorization"] == "Bearer " + TOKEN
        assert json.loads(received[0]["body"]) == payload
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err + caplog.text
