"""HTTP surface: heartbeat ingestion for agents, dashboard and JSON for the operator."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import signal
import threading
import time
import urllib.parse
from dataclasses import dataclass
from html import escape as html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .config import Config
from .desired import desired_state
from .heartbeat import HeartbeatError, validate_heartbeat
from .monitor import Monitor
from .passwords import verify_password
from .render import build_rows, render_add_result, render_dashboard
from .store import Store, StoreError

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


@dataclass(frozen=True)
class Identity:
    """Who is asking. ``owner`` is empty for an admin, who sees the whole fleet."""

    role: str
    owner: str
    label: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


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


def identify(header: Optional[str], cfg: Config, store: Store) -> Optional[Identity]:
    """Resolve credentials to an Identity, or None.

    The admin token keeps working exactly as before, with any user name, because
    it is the operator's key and existing scripts and bookmarks depend on it. A
    named account is only consulted when the password is not the admin token, so
    a user cannot shadow the operator by choosing a clever name.
    """
    if _admin_ok(header, cfg):
        return Identity("admin", "", "admin token")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True)
        username, sep, password = decoded.decode("utf-8", "replace").partition(":")
    except (ValueError, TypeError):
        return None
    if not sep or not username:
        return None
    record = store.get_user(username)
    if record is None:
        # Spend the same work on an unknown user as on a known one, so the
        # response time does not say which names exist.
        verify_password(password, "pbkdf2_sha256$600000$00$00")
        return None
    if not verify_password(password, record["password_hash"]):
        return None
    role = record["role"] if record["role"] in ("admin", "owner") else "owner"
    return Identity(role, "" if role == "admin" else record["owner"], username)


def csrf_token(cfg: Config) -> str:
    """Form token for the console's write actions.

    Derived from the admin token, so it cannot be computed by a third-party page.
    Browsers attach Basic credentials automatically, which is exactly what makes a
    cross-site POST possible, and this is what stops it.
    """
    return hmac.new(cfg.admin_token.encode("utf-8"), b"ccfleet-csrf",
                    hashlib.sha256).hexdigest()


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

        def _identity(self) -> Optional[Identity]:
            """Any valid account. Returns None and answers 401 when there is none."""
            who = identify(self.headers.get("Authorization"), ctx.cfg, ctx.store)
            if who is not None:
                return who
            self._json(401, {"error": "unauthorized"},
                       {"WWW-Authenticate": 'Basic realm="ccfleet", charset="UTF-8"'})
            return None

        def _require_admin(self) -> bool:
            """For anything that changes the fleet. An owner gets 403, not 401:
            their credentials were fine, the action is not theirs to take."""
            who = self._identity()
            if who is None:
                return False
            if who.is_admin:
                return True
            self._json(403, {"error": "this account cannot manage nodes"})
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
                who = self._identity()
                if who is not None:
                    self._send(200, self._dashboard(who).encode("utf-8"), HTML_HEADERS)
            elif path == "/api/nodes":
                who = self._identity()
                if who is not None:
                    self._json(200, {"nodes": self._rows(who)})
            elif path == "/api/alerts":
                who = self._identity()
                if who is not None:
                    self._json(200, {"alerts": self._scope_alerts(ctx.store.open_alerts(), who),
                                     "recent": self._scope_alerts(ctx.store.recent_alerts(), who)})
            else:
                self._json(404, {"error": "not found"})

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def _form(self) -> Optional[dict[str, str]]:
            body = self._read_body()
            if body is None:
                return None
            parsed = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
            return {k: v[0] for k, v in parsed.items()}

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path.startswith("/actions/"):
                self._console_action(path)
                return
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
            events = ctx.monitor.record_heartbeat(node, payload, time.time())
            self._json(200, {
                "ok": True,
                # Kept for agents predating the desired block; same value, new home.
                "pinned_version": node["pinned_version"],
                "desired": desired_state(node),
                "open_alerts": [a["rule"] for a in ctx.store.open_alerts(node["id"])],
                "events": len(events)})

        # -- console actions -----------------------------------------------

        def _console_action(self, path: str) -> None:
            if not self._require_admin():
                return
            form = self._form()
            if form is None:
                return
            if not hmac.compare_digest(form.get("csrf", ""), csrf_token(ctx.cfg)):
                self._json(403, {"error": "bad or missing csrf token"})
                return
            parts = path.strip("/").split("/")          # actions/node/<id|add>/<action>
            try:
                if parts[:3] == ["actions", "node", "add"]:
                    self._action_add(form)
                elif len(parts) == 4 and parts[:2] == ["actions", "node"]:
                    self._action_on_node(parts[2], parts[3], form)
                else:
                    self._json(404, {"error": "not found"})
            except StoreError as exc:
                page = f'<!doctype html><p>{html_escape(str(exc))}</p><p><a href="/">back</a></p>'
                self._send(400, page.encode("utf-8"), HTML_HEADERS)

        def _action_add(self, form: dict[str, str]) -> None:
            token = ctx.store.add_node(
                form.get("node_id", "").strip(), form.get("owner", "").strip(),
                form.get("region", "").strip(), form.get("pinned_version", "").strip(),
                form.get("rc_expected") == "1", now=time.time())
            body = render_add_result(form["node_id"].strip(), token, ctx.cfg,
                                     form.get("owner", "").strip())
            self._send(200, body.encode("utf-8"), HTML_HEADERS)

        def _action_on_node(self, node_id: str, action: str, form: dict[str, str]) -> None:
            if action == "enable":
                ctx.store.set_enabled(node_id, True)
            elif action == "disable":
                ctx.store.set_enabled(node_id, False)
            elif action == "rc-on":
                ctx.store.set_rc_expected(node_id, True)
            elif action == "rc-off":
                ctx.store.set_rc_expected(node_id, False)
            elif action == "pin":
                ctx.store.set_pinned_version(node_id, form.get("version", "").strip())
            elif action == "rotate-token":
                token = ctx.store.rotate_token(node_id)
                node = ctx.store.get_node(node_id) or {}
                self._send(200, render_add_result(node_id, token, ctx.cfg,
                                                  node.get("owner", "")).encode("utf-8"),
                           HTML_HEADERS)
                return
            elif action == "remove":
                if form.get("confirm") != node_id:
                    self._json(400, {"error": "confirmation did not match the node id"})
                    return
                ctx.store.remove_node(node_id)
            else:
                self._json(404, {"error": "unknown action"})
                return
            self._redirect("/")

        # -- views ---------------------------------------------------------

        def _visible_nodes(self, who: Identity) -> list[dict[str, Any]]:
            nodes = ctx.store.list_nodes()
            if who.is_admin:
                return nodes
            return [n for n in nodes if n.get("owner") == who.owner]

        def _scope_alerts(self, alerts: list[dict[str, Any]],
                          who: Identity) -> list[dict[str, Any]]:
            if who.is_admin:
                return alerts
            mine = {n["id"] for n in self._visible_nodes(who)}
            return [a for a in alerts if a.get("node_id") in mine]

        def _rows(self, who: Identity) -> list[dict[str, Any]]:
            return build_rows(self._visible_nodes(who), ctx.store.latest_heartbeats(),
                              ctx.store.open_alerts(), time.time())

        def _dashboard(self, who: Identity) -> str:
            # An owner is handed no CSRF token, because there is nothing for them
            # to submit. That makes the absence of the forms structural rather
            # than cosmetic: even a hand-built POST is refused by _require_admin.
            csrf = csrf_token(ctx.cfg) if who.is_admin else ""
            return render_dashboard(self._rows(who),
                                    self._scope_alerts(ctx.store.open_alerts(), who),
                                    time.time(), ctx.cfg, csrf, who)

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
