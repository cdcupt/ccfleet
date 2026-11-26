import contextlib
import http.client
import json
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import pytest

from ccfleet_agent import home_mac as client
from ccfleetd import api, home_macs, sessions, usersite
from ccfleetd.config import Config
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import Store


@pytest.fixture
def service(tmp_path, monkeypatch):
    import tempfile
    from pathlib import Path

    short_dir = tempfile.TemporaryDirectory(prefix="hm-", dir="/tmp")
    database = str(Path(short_dir.name) / "accounts.db")
    store = Store(database)
    for owner in ("alice", "bob", "outside"):
        store.add_account(owner, owner, owner + "@example.test", now=time.time())
    import subprocess

    from ccfleetd import home_mac_bundle
    from tools.build_home_mac_release import build

    signer = tmp_path / "test-release-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-C", "", "-N", "", "-f", str(signer)], check=True
    )
    monkeypatch.setattr(
        home_mac_bundle, "PUBLIC_KEY", signer.with_suffix(".pub").read_text().strip()
    )
    local_release = tmp_path / "signed"
    build(local_release, "https://fleet.test", "0.1.0", signer)
    cfg = Config(
        db_path=database,
        home_mac_release_dir=str(local_release),
        public_url="https://fleet.test",
        home_mac_owners=("alice", "bob"),
        cookie_secret="s" * 32,
        google_client_id="fixture",
        google_client_secret="fixture",
        cookie_secure=False,
    )
    ctx = api.Context(store, cfg, Monitor(store, cfg, LogNotifier()))
    server = api.build_server(ctx, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cookies = {}
    for owner in ("alice", "bob", "outside"):
        sid = store.create_session(owner, now=time.time(), ttl_s=600)
        cookies[owner] = (
            sessions.COOKIE_NAME + "=" + sessions.sign(sid, cfg.cookie_secret),
            usersite.csrf_for(sid, cfg.cookie_secret),
        )

    def request(path, owner=None, fields=None, headers=None, src=None):
        h = {"Host": "fleet.test", **(headers or {})}
        if owner:
            h["Cookie"] = cookies[owner][0]
        body = None
        if fields is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
            body = urllib.parse.urlencode(fields)
        conn = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=5, source_address=src
        )
        try:
            conn.request("POST" if fields is not None else "GET", path, body, h)
            reply = conn.getresponse()
            return reply.status, reply.read().decode(), dict(reply.getheaders())
        finally:
            conn.close()

    request.origin = "http://127.0.0.1:" + str(server.server_address[1])
    try:
        yield store, cfg, request, cookies, database
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        store.close()
        short_dir.cleanup()


def generation(tmp_path, monkeypatch, request):
    token, keys = client.identity(tmp_path)
    sent = {}

    def post(_origin, action, token, fields):
        sent.update(fields)
        code, raw, _ = request(
            home_macs.API + action, fields=fields, headers={"Authorization": "Bearer " + token}
        )
        assert code == 200, raw
        return json.loads(raw)

    monkeypatch.setattr(client, "post", post)
    result = client.enroll(tmp_path, "https://fleet.test", token, keys, "macowner")
    return token, result["code"], sent


def test_http_lifecycle(service, tmp_path, monkeypatch):
    store, cfg, request, cookies, _ = service
    page = home_macs.PAGE
    status, _, headers = request(page)
    assert status == 303
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(headers["Location"]).query)["next"] == [page]
    assert request(page, "outside")[0] == 404
    assert 'href="/account/machines"' in request("/en/account", "alice")[1]
    assert request("/zh" + page, "alice")[0] == 200
    status, html, _ = request("/en" + page, "alice")
    assert status == 200 and "Download Mac connector" in html
    status, script, headers = request("/downloads/ccfleet-home-mac.command")
    assert (
        status == 200
        and "https://fleet.test" in script
        and "attachment" in headers["Content-Disposition"]
    )
    token, code, fields = generation(tmp_path, monkeypatch, request)
    auth = {"Authorization": "Bearer " + token}
    again = json.loads(request(home_macs.API + "begin", fields=fields, headers=auth)[1])
    assert again["code"] == code
    assert (
        request(home_macs.API + "begin", fields={**fields, "unix_user": "another"}, headers=auth)[0]
        == 403
    )
    approval = {
        "csrf": cookies["alice"][1],
        "code": code[:5] + "-" + code[5:],
        "name": "Home <Mac>",
    }
    assert request(page + "/approve", "alice", {**approval, "csrf": "wrong"})[0] == 403
    assert request(page + "/approve", "alice", approval)[0] == 303
    assert home_macs.for_owner(store, "alice", time.time())["state"] == "waiting"
    assert request(page + "/approve", "bob", {**approval, "csrf": cookies["bob"][1]})[0] == 400
    first = request(home_macs.API + "check-in", fields={}, headers=auth)
    assert first[0] == 200
    node = json.loads(first[1])["node_id"]
    assert store.get_slot(node)["held_by"] == "alice"
    assert store.get_slot(node)["kind"] == "owner"
    assert store.get_node(node)["public_status_number"] is None
    assert "Home &lt;Mac&gt;" in request(page, "alice")[1]
    assert "Home &lt;Mac&gt;" not in request(page, "bob")[1]
    assert home_macs.for_owner(store, "alice", time.time() + 121)["state"] == "offline"
    assert (
        json.loads(request(home_macs.API + "check-in", fields={}, headers=auth)[1])["node_id"]
        == node
    )
    registration = home_macs.for_owner(store, "alice", time.time())["id"]
    assert (
        request(page + "/remove", "bob", {"csrf": cookies["bob"][1], "node_id": registration})[0]
        == 403
    )
    assert (
        request(page + "/remove", "alice", {"csrf": cookies["alice"][1], "node_id": registration})[
            0
        ]
        == 303
    )
    assert request(home_macs.API + "check-in", fields={}, headers=auth)[0] == 403
    assert not store.get_node(node)["enabled"]
    assert home_macs.for_owner(store, "alice", time.time()) is None


def test_one_code_one_owner_and_expiry(service, tmp_path, monkeypatch):
    store, cfg, request, cookies, database = service
    token, code, fields = generation(tmp_path, monkeypatch, request)
    with pytest.raises(home_macs.Error):
        home_macs.approve(store, "alice", code, "Test", time.time() + 610)

    def approve(owner):
        with contextlib.closing(Store(database)) as other:
            try:
                home_macs.approve(other, owner, code, "Test", time.time())
                return True
            except home_macs.Error:
                return False

    with ThreadPoolExecutor(2) as executor:
        assert sorted(executor.map(approve, ("alice", "bob"))) == [False, True]
    assert not store.node_for_token(token)


@pytest.mark.skipif(
    __import__("sys").platform != "darwin", reason="exercises Apple's installed sshd"
)
def test_real_pinned_ssh_over_outbound_mac_stream(service, tmp_path, monkeypatch):
    """Use the actual downloader, reverse channel and inner OpenSSH from a fresh identity."""
    import base64
    import io
    import os
    import pwd
    import subprocess
    import sys
    import zipfile

    store, cfg, request, cookies, database = service
    token, code, _ = generation(tmp_path, monkeypatch, request)
    home_macs.approve(store, "alice", code, "Network test", time.time())
    reg = home_macs.for_owner(store, "alice", time.time())["id"]
    _, raw, _ = request("/downloads/ccfleet-home-mac.command")
    import ast

    values = {
        x.targets[0].id: ast.literal_eval(x.value)
        for x in ast.parse(raw).body
        if isinstance(x, ast.Assign)
        and isinstance(x.targets[0], ast.Name)
        and isinstance(x.value, ast.Constant)
    }
    runtime = tmp_path / "runtime"
    zipfile.ZipFile(io.BytesIO(base64.b64decode(values["BUNDLE"]))).extractall(runtime)
    monkeypatch.syspath_prepend(str(runtime))
    import home_mac_runtime as host

    user = pwd.getpwuid(os.getuid()).pw_name
    config = host.sshd_config(tmp_path, user, client.write)
    key = tmp_path / "fixture-user"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    (tmp_path / "authorized_keys").write_text(
        'restrict,command="/bin/echo fleet-home-mac-ok" ' + key.with_suffix(".pub").read_text()
    )
    (tmp_path / "known").write_text("mac " + (tmp_path / "slot_host.pub").read_text())
    auth = {"Authorization": "Bearer " + token}
    origin = request.origin
    claim = home_macs.check_in(store, token, time.time())["node_id"]
    assert store.slot_inference_allowed(claim, "alice")
    assert not store.slot_inference_allowed(claim, "bob")
    assert store.held_slot_count("alice") == 0

    def next_stream(exe):
        until = time.monotonic() + 6
        while time.monotonic() < until:
            ticket = json.loads(request(home_macs.API + "wait", fields={}, headers=auth)[1])[
                "ticket"
            ]
            if ticket:
                return exe.submit(host.serve_stream, origin, token, ticket, config)
            time.sleep(0.05)
        pytest.fail("Mac received no stream")

    with ThreadPoolExecutor(3) as exe:
        for _ in range(2):
            ssh = exe.submit(
                subprocess.run,
                [
                    "ssh",
                    "-F",
                    "/dev/null",
                    "-T",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "HostKeyAlias=mac",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    "UserKnownHostsFile=" + str(tmp_path / "known"),
                    "-i",
                    str(key),
                    "-o",
                    "ProxyCommand="
                    + sys.executable
                    + " -m ccfleetd.home_mac_proxy "
                    + database
                    + " "
                    + reg,
                    user + "@mac",
                    "ccfleet-inference-v1",
                ],
                capture_output=True,
                timeout=15,
            )
            worker = next_stream(exe)
            result = ssh.result(timeout=15)
            assert result.returncode == 0, result.stderr.decode()
            assert result.stdout.strip() == b"fleet-home-mac-ok"
            worker.result(timeout=5)

    # Exercise the real gateway SSH command and data framing with a local model fixture.
    from dataclasses import replace

    from ccfleetd.gateway_transport import SSHAttempt

    responder = tmp_path / "respond.py"
    responder.write_text("""import json, struct, sys
source, output = sys.stdin.buffer, sys.stdout.buffer
def field():
    size=struct.unpack('!I',source.read(4))[0]
    return json.loads(source.read(size))
def frame(raw):
    return struct.pack('!I',len(raw))+raw
def reply(raw):
    meta={'version':2,'status':200,'headers':{'content-type':'application/json'},'retryable':False}
    output.write(frame(json.dumps(meta).encode())+frame(raw)+frame(b''))
    output.flush()
assert field()['operation']=='status'
reply(b'{"ready":true,"protocol":2}')
request=field()
assert source.read(request['body_size'])==b'local-fixture-request'
reply(b'local-fixture-reply')
""")
    (tmp_path / "authorized_keys").write_text(
        f'restrict,command="/usr/bin/python3 -I {responder}" ' + key.with_suffix(".pub").read_text()
    )
    node = store.get_node(claim)
    target = {**node, "unix_user": user}
    with ThreadPoolExecutor(3) as exe:

        def gateway():
            attempt = SSHAttempt(target, replace(cfg, gateway_ssh_key_path=str(key)))
            try:
                attempt.start({}, b"local-fixture-request")
                assert attempt.metadata()["status"] == 200
                data = b"".join(attempt.chunks())
                attempt.finish()
                return data
            finally:
                attempt.close()

        reply = exe.submit(gateway)
        worker = next_stream(exe)
        assert reply.result(timeout=12) == b"local-fixture-reply"
        worker.result(timeout=4)

        # An actual paired CLI stream is disconnected when the owner removes the Mac.
        pairing = store.request_cli_pairing(claim, "alice", now=time.time())
        device = store.register_cli_device(
            pairing, key.with_suffix(".pub").read_text().strip(), "test laptop", now=time.time()
        )
        opened = exe.submit(
            host.fleet_wire.open_websocket,
            origin.replace("http://", "ws://") + "/api/cli/connect",
            device["device_token"],
        )
        worker = next_stream(exe)
        transport, initial = opened.result(timeout=8)
        with transport:
            transport.settimeout(6)
            reader = host.fleet_wire.FrameReader(transport, initial)
            opcode, banner = reader.read()
            assert opcode == 2 and banner.startswith(b"SSH-2.0")
            home_macs.remove(store, "alice", reg, time.time())
            try:
                assert reader.read()[0] == 8
            except EOFError:
                pass
        with contextlib.suppress(host.fleet_wire.CliError, EOFError, OSError):
            worker.result(timeout=5)
        assert store.resolve_cli_device(device["device_token"], now=time.time()) is None


def test_same_tls_origin_reconnects_from_ipv4_and_ipv6(service, tmp_path, monkeypatch):
    """The same hostname, key, slot and account survive two distinct source addresses."""
    import http.server
    import socket
    import ssl
    import subprocess

    store, cfg, request, cookies, _ = service
    cert = tmp_path / "local.crt"
    key = tmp_path / "local.key"
    config = tmp_path / "openssl.cnf"
    config.write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=x\nprompt=no\n"
        "[dn]\nCN=localhost\n[x]\nsubjectAltName=DNS:localhost\n"
        "basicConstraints=critical,CA:TRUE\nkeyUsage=keyCertSign,digitalSignature\n"
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-x509",
            "-sha256",
            "-days",
            "1",
            "-config",
            str(config),
            "-out",
            str(cert),
            "-keyout",
            str(key),
        ],
        capture_output=True,
        check=True,
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    ctx = api.Context(store, cfg, Monitor(store, cfg, LogNotifier()))
    addresses = set()
    base = api.make_handler(ctx)

    class Handler(base):
        def do_POST(self):
            addresses.add(self.client_address[0])
            super().do_POST()

    class IPv6Server(http.server.ThreadingHTTPServer):
        address_family = socket.AF_INET6

        def server_bind(self):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            super().server_bind()

    with contextlib.ExitStack() as stack:
        ipv4 = stack.enter_context(http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        port = ipv4.server_address[1]
        ipv6 = stack.enter_context(IPv6Server(("::1", port), Handler))
        for server in (ipv4, ipv6):
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            stack.callback(server.shutdown)
        origin = "https://localhost:" + str(port)
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        # Apple's system Python also consults Keychain. Trust only this fixture CA.
        monkeypatch.setattr(
            ssl,
            "_create_default_https_context",
            lambda: ssl.create_default_context(cafile=str(cert)),
        )
        monkeypatch.setenv("no_proxy", "localhost")
        lookup = socket.getaddrinfo
        family = socket.AF_INET

        def resolve(host, port, *args, **kwargs):
            if host == "localhost":
                return lookup(
                    "127.0.0.1" if family == socket.AF_INET else "::1", port, *args, **kwargs
                )
            return lookup(host, port, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        token, keys = client.identity(tmp_path)
        pending = client.enroll(tmp_path, origin, token, keys, "macowner")
        assert (
            request(
                home_macs.PAGE + "/approve",
                "alice",
                {"csrf": cookies["alice"][1], "name": "Home Mac", "code": pending["code"]},
            )[0]
            == 303
        )
        one = client.check_in(tmp_path, origin, token)
        before = store.get_slot(one["node_id"])
        family = socket.AF_INET6
        two = client.check_in(tmp_path, origin, token)
        after = store.get_slot(two["node_id"])
        assert one == two
        assert before["gateway_claim_id"] == after["gateway_claim_id"]
        assert before["gateway_generation"] == after["gateway_generation"]
        assert addresses == {"127.0.0.1", "::1"}


def test_tampered_release_is_not_served(service):
    from pathlib import Path

    from ccfleetd import home_mac_bundle as bundle

    _, cfg, request, _, _ = service
    assert request("/downloads/" + bundle.MANIFEST)[0] == 200
    assert request("/downloads/" + bundle.SIGNATURE)[0] == 200
    root = Path(cfg.home_mac_release_dir)
    (root / bundle.COMMAND).write_bytes(b"changed")
    bundle.verified_assets.cache_clear()
    assert request("/downloads/" + bundle.COMMAND)[0] == 503


def test_download_installs_user_job_in_an_isolated_home(service, tmp_path, monkeypatch):
    """Exercise the downloaded installer with a test home and capture launchd control calls."""
    import os
    import plistlib
    import runpy
    import subprocess

    _, cfg, request, _, _ = service
    status, body, _ = request("/downloads/ccfleet-home-mac.command")
    assert status == 200
    program = tmp_path / "installer.command"
    program.write_text(body)
    module = runpy.run_path(str(program))
    home = tmp_path / "fresh-home"
    home.mkdir(mode=0o700)
    folder = module["private_root"](home)
    invocations = []

    def launchctl(args, **kwargs):
        assert args[0] == "/bin/launchctl"
        invocations.append(args)
        return subprocess.CompletedProcess(args, 1 if args[1] == "print" else 0)

    monkeypatch.setattr(subprocess, "run", launchctl)
    module["install_agent"](home, folder)
    job = plistlib.loads((home / "Library/LaunchAgents/io.ccfleet.home-mac.plist").read_bytes())
    assert job["ProgramArguments"] == [
        "/usr/bin/python3",
        "-I",
        "-B",
        str(folder / "connector.py"),
        "--check-in",
    ]
    assert (folder / "connector.py").read_text() == body
    assert (module["runtime_dir"](home) / "mac_slot_entry.py").is_file()
    assert invocations[-1][1:3] == ["bootstrap", f"gui/{os.getuid()}"]
    assert not (home / ".ssh").exists()
