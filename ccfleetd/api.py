"""HTTP surface: heartbeat ingestion for agents, dashboard and JSON for the operator."""

from __future__ import annotations

import base64
import hmac
import json
import logging
import re
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .config import Config
from .heartbeat import HeartbeatError, validate_heartbeat
from .monitor import Monitor
from .render import build_rows, render_dashboard
from .store import Store

log = logging.getLogger("ccfleetd.api")

NODE_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
HTML_HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
}
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}


class Context:
    def __init__(self, store: Store, cfg: Config, monitor: Monitor) -> None:
        self.store = store
        self.cfg = cfg
        self.monitor = monitor


def _admin_ok(header: Optional[str], cfg: Config) -> bool:
    """Accept HTTP Basic (any user name, admin token as password) or a Bearer admin token."""
    if not header or not cfg.admin_token:
        return False
    scheme, _, value = header.partition(" ")
    expected = cfg.admin_token.encode("utf-8")
    if scheme.lower() == "bearer":
        return hmac.compare_digest(value.strip().encode("utf-8"), expected)
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(value.strip(), validate=True)
        except (ValueError, TypeError):
            return False
        _user, _, password = decoded.partition(b":")
        return hmac.compare_digest(password, expected)
    return False


def _bearer_token(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not NODE_TOKEN_RE.match(value):
        return None
    return value


def make_handler(ctx: Context) -> type[BaseHTTPRequestHandler]:
    class FleetHandler(BaseHTTPRequestHandler):
        server_version = "ccfleetd"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
            log.info("%s %s", self.address_string(), fmt % args)

        # -- helpers -------------------------------------------------------

        def _send(self, status: int, body: bytes, headers: dict[str, str]) -> None:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: Any, extra: Optional[dict[str, str]] = None) -> None:
            headers = dict(JSON_HEADERS)
            if extra:
                headers.update(extra)
            self._send(status, json.dumps(payload).encode("utf-8"), headers)

        def _require_admin(self) -> bool:
            if _admin_ok(self.headers.get("Authorization"), ctx.cfg):
                return True
            self._json(401, {"error": "unauthorized"},
                       {"WWW-Authenticate": 'Basic realm="ccfleet", charset="UTF-8"'})
            return False

        def _read_body(self) -> Optional[bytes]:
            raw_len = self.headers.get("Content-Length")
            if raw_len is None or not raw_len.isdigit():
                self._json(411, {"error": "content-length required"})
                return None
            length = int(raw_len)
            if length > ctx.cfg.max_body_bytes:
                self._json(413, {"error": "body too large"})
                return None
            return self.rfile.read(length)

        # -- routes --------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                self._json(200, {"ok": True})
            elif path == "/":
                if self._require_admin():
                    self._send(200, self._dashboard().encode("utf-8"), HTML_HEADERS)
            elif path == "/api/nodes":
                if self._require_admin():
                    self._json(200, {"nodes": self._rows()})
            elif path == "/api/alerts":
                if self._require_admin():
                    self._json(200, {"alerts": ctx.store.open_alerts(),
                                     "recent": ctx.store.recent_alerts()})
            else:
                self._json(404, {"error": "not found"})

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path != "/api/heartbeat":
                self._json(404, {"error": "not found"})
                return
            token = _bearer_token(self.headers.get("Authorization"))
            node = ctx.store.node_for_token(token) if token else None
            if node is None:
                self._json(401, {"error": "unauthorized"})
                return
            body = self._read_body()
            if body is None:
                return
            try:
                payload = validate_heartbeat(json.loads(body.decode("utf-8")), node["id"])
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"error": "body must be UTF-8 JSON"})
                return
            except HeartbeatError as exc:
                self._json(400, {"error": str(exc)})
                return
            now = time.time()
            ctx.store.insert_heartbeat(node["id"], now, payload)
            events = ctx.monitor.check_node(node, now)
            self._json(200, {"ok": True, "pinned_version": node["pinned_version"],
                             "open_alerts": [a["rule"] for a in ctx.store.open_alerts(node["id"])],
                             "events": len(events)})

        # -- views ---------------------------------------------------------

        def _rows(self) -> list[dict[str, Any]]:
            return build_rows(ctx.store.list_nodes(), ctx.store.latest_heartbeats(),
                              ctx.store.open_alerts(), time.time())

        def _dashboard(self) -> str:
            return render_dashboard(self._rows(), ctx.store.open_alerts(), time.time(), ctx.cfg)

    return FleetHandler


def build_server(ctx: Context, host: Optional[str] = None,
                 port: Optional[int] = None) -> ThreadingHTTPServer:
    host = ctx.cfg.bind_host if host is None else host
    port = ctx.cfg.bind_port if port is None else port
    server = ThreadingHTTPServer((host, port), make_handler(ctx))
    server.daemon_threads = True
    return server


def serve(ctx: Context) -> None:
    """Run the HTTP server and the periodic monitor until SIGINT/SIGTERM."""
    server = build_server(ctx)
    stop = threading.Event()
    worker = threading.Thread(target=ctx.monitor.run_forever, args=(stop,),
                              name="ccfleet-monitor", daemon=True)
    worker.start()

    def _shutdown(_signum: int, _frame: Any) -> None:
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    log.info("ccfleetd listening on http://%s:%d", ctx.cfg.bind_host, ctx.cfg.bind_port)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
