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

from . import oauth, sessions
from .config import Config
from .desired import desired_state
from .heartbeat import HeartbeatError, validate_heartbeat
from .monitor import Monitor
from .passwords import verify_password
from .render import (
    build_rows,
    render_account,
    render_add_result,
    render_dashboard,
    render_token_result,
)
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


def _safe_next(raw: str) -> str:
    """Where to land after signing in, if it is somewhere on this site.

    Anything with a scheme or a host is dropped, and so is `//evil.example`,
    which a browser reads as protocol-relative and follows off-site. An open
    redirect on a sign-in route is how a link that genuinely starts at our
    domain ends up somewhere else with the person already authenticated.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return ""
    if "\\" in raw or "\n" in raw or "\r" in raw:
        return ""
    return raw


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

        # -- signing in with Google ----------------------------------------

        def _signed_in(self) -> Optional[dict[str, Any]]:
            """The account this browser is signed in as, or None.

            Separate from _identity(): that is the operator's basic auth, which
            is the console's own door. These are the people who rent slots, and
            the two must not be able to stand in for one another.
            """
            if not ctx.cfg.cookie_secret:
                return None
            raw = sessions.read_cookie(self.headers.get("Cookie"))
            if raw is None:
                return None
            try:
                session_id = sessions.unsign(raw, ctx.cfg.cookie_secret)
            except sessions.SessionError:
                return None
            return ctx.store.account_for_session(session_id, now=time.time())

        def _sign_in_start(self) -> None:
            if not ctx.cfg.google_ready:
                self._json(503, {"error": "sign-in is not configured"})
                return
            state, verifier = oauth.new_state(), oauth.new_verifier()
            # Only our own paths, and only paths. An open redirect here would
            # let somebody send a ccfleet sign-in link that lands on their site
            # with whatever the browser was carrying.
            wanted = _safe_next(urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).get("next", [""])[0])
            ctx.store.begin_oauth_flow(state, verifier, now=time.time(),
                                       ttl_s=oauth.FLOW_TTL_S, next_url=wanted)
            # The state also goes into a cookie, and that is the half that
            # actually ties the callback to a browser. Held only on the server
            # it proves the flow was started by *somebody* — so an attacker can
            # start one, sign in as themselves, and send the victim the
            # resulting callback URL, which signs the victim into the
            # attacker's account. Requiring the cookie means only the browser
            # that began the flow can finish it.
            self._redirect_with_cookies(
                oauth.authorize_url(client_id=ctx.cfg.google_client_id,
                                    redirect_uri=ctx.cfg.redirect_uri,
                                    state=state, verifier=verifier),
                [sessions.cookie_header(
                    sessions.sign(state, ctx.cfg.cookie_secret),
                    ttl_s=oauth.FLOW_TTL_S, secure=ctx.cfg.cookie_secure,
                    name=sessions.FLOW_COOKIE_NAME)])

        def _sign_in_callback(self) -> None:
            if not ctx.cfg.google_ready:
                self._json(503, {"error": "sign-in is not configured"})
                return
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            state = query.get("state", [""])[0]
            # The cookie first: this is the check that makes `state` mean
            # anything. Without it a callback URL works in any browser.
            raw_flow = sessions.read_cookie(self.headers.get("Cookie"),
                                            sessions.FLOW_COOKIE_NAME)
            try:
                from_browser = (sessions.unsign(raw_flow, ctx.cfg.cookie_secret)
                                if raw_flow else "")
            except sessions.SessionError:
                from_browser = ""
            if (not state or not from_browser
                    or not sessions.matches(state, from_browser)):
                self._sign_in_failed("that sign-in did not start in this "
                                     "browser; please start again")
                return
            # Taken exactly once. A callback with no matching flow is either a
            # replay or somebody else's link, and neither may sign anybody in.
            flow = ctx.store.take_oauth_flow(state, now=time.time())
            if flow is None:
                self._sign_in_failed("that sign-in link has expired; start again")
                return
            if query.get("error"):
                # The person pressed cancel on Google's consent screen.
                self._redirect_with_cookies("/?signin=cancelled",
                                            [self._clear_flow_cookie()])
                return
            try:
                token = oauth.exchange_code(
                    code=query.get("code", [""])[0], verifier=flow["verifier"],
                    client_id=ctx.cfg.google_client_id,
                    client_secret=ctx.cfg.google_client_secret,
                    redirect_uri=ctx.cfg.redirect_uri)
                who = oauth.fetch_identity(token)
            except oauth.OAuthError as exc:
                # Logged in full, shown as a sentence. The reasons name our
                # configuration, which is not the visitor's business.
                log.warning("google sign-in failed: %s", exc)
                self._sign_in_failed("Google could not confirm that sign-in. "
                                     "Please try again.", status=502)
                return
            account = ctx.store.upsert_account_from_google(
                who["sub"], who["email"], now=time.time())
            session_id = ctx.store.create_session(
                account["id"], now=time.time(), ttl_s=ctx.cfg.session_ttl_s)
            self._redirect_with_cookies(
                flow["next_url"] or "/",
                [sessions.cookie_header(
                    sessions.sign(session_id, ctx.cfg.cookie_secret),
                    ttl_s=ctx.cfg.session_ttl_s, secure=ctx.cfg.cookie_secure),
                 self._clear_flow_cookie()])

        def _sign_out(self) -> None:
            raw = sessions.read_cookie(self.headers.get("Cookie"))
            if raw and ctx.cfg.cookie_secret:
                try:
                    ctx.store.end_session(
                        sessions.unsign(raw, ctx.cfg.cookie_secret))
                except sessions.SessionError:
                    pass
            # Cleared whatever happened, so a cookie we cannot read still goes.
            self._redirect_with_cookies(
                "/", [sessions.clearing_header(secure=ctx.cfg.cookie_secure)])

        def _clear_flow_cookie(self) -> str:
            """The half-finished sign-in is over, however it ended."""
            return sessions.clearing_header(secure=ctx.cfg.cookie_secure,
                                            name=sessions.FLOW_COOKIE_NAME)

        def _sign_in_failed(self, message: str, status: int = 400) -> None:
            """Refuse, and take the flow cookie with it so the next attempt
            starts clean rather than against a state that is already spent."""
            body = json.dumps({"error": message}).encode("utf-8")
            headers = dict(JSON_HEADERS)
            headers["Set-Cookie"] = self._clear_flow_cookie()
            self._send(status, body, headers)

        def _redirect_with_cookies(self, location: str,
                                   cookies: list[str]) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()

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
            elif path == "/account":
                who = self._signed_in()
                self._send(200, render_account(
                    who, ctx.store.held_slot_count(who["id"]) if who else 0,
                    ctx.cfg).encode("utf-8"), HTML_HEADERS)
            elif path == "/auth/google/start":
                self._sign_in_start()
            elif path == "/auth/google/callback":
                self._sign_in_callback()
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

        # Which part of the page an action belongs to. A POST redirects to the
        # fleet page, and without a fragment that means the top of it — so every
        # press of a button two screens down sent you back up to scroll to it
        # again, mid-task, with a code in your clipboard.
        ACTION_ANCHORS = {
            "token-start": "device-tokens",
            "token-show": "device-tokens",
            "token-done": "device-tokens",
            "login-start": "sign-in",
            "login-code": "sign-in",
            "login-cancel": "sign-in",
            # The manage card is the furthest down of all of them, and these
            # are the buttons most likely to be pressed several times in a row.
            "enable": "manage",
            "disable": "manage",
            "rc-on": "manage",
            "rc-off": "manage",
            "pin": "manage",
            "remove": "manage",
        }

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/auth/signout":
                self._sign_out()
                return
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
            now = time.time()
            login = (payload.get("reconcile") or {}).get("login")
            if isinstance(login, dict) and login.get("state"):
                requested_at = login.get("requested_at")
                ctx.store.record_login_progress(
                    node["id"], str(login.get("state")), str(login.get("url") or ""),
                    str(login.get("detail") or ""), now,
                    requested_at if isinstance(requested_at, (int, float))
                    and not isinstance(requested_at, bool) else None,
                    secret=str(login.get("secret") or ""))
                # Consumed. The store redacts it before anything is archived,
                # which is the guarantee; dropping it here as well keeps a
                # credential from travelling further through this process than
                # the one call that needed it.
                login.pop("secret", None)
            # Before the alerts are evaluated and before the reply is built, so
            # both see the slots as this report left them: a wipe confirmed
            # here is a free slot in the same reply, not one beat later.
            if "slots" in payload:
                ctx.store.apply_slot_report(node["id"], payload["slots"], now=now)
            events = ctx.monitor.record_heartbeat(node, payload, now)
            self._json(200, {
                "ok": True,
                # Kept for agents predating the desired block; same value, new home.
                "pinned_version": node["pinned_version"],
                "desired": desired_state(node, ctx.store.get_login(node["id"]),
                                         ctx.store.list_slots(node_id=node["id"])),
                "open_alerts": [a["rule"] for a in ctx.store.open_alerts(node["id"])],
                "events": len(events)})

        # -- console actions -----------------------------------------------

        # Signing in is the one thing an owner must be able to do for themselves:
        # the whole point is that they no longer need SSH to reach their node, and
        # routing it through the operator would just move the bottleneck. These
        # act only on a node the caller already owns; everything else stays admin.
        # A device token is the owner's own credential for their own machines,
        # minted from the account their node already holds. Needing an operator
        # to press it would move exactly the bottleneck this removes.
        OWNER_ACTIONS = ("login-start", "login-code", "login-cancel",
                         "token-start", "token-show", "token-done")

        def _may_act_on(self, node_id: str, action: str) -> bool:
            """Authorisation for one action on one node."""
            who = self._identity()
            if who is None:
                return False                       # already answered 401
            if who.is_admin:
                return True
            node = ctx.store.get_node(node_id)
            if action in self.OWNER_ACTIONS and node is not None and node["owner"] == who.owner:
                return True
            # Their credentials were fine; the action is not theirs to take.
            self._json(403, {"error": "this account cannot manage that node"})
            return False

        def _console_action(self, path: str) -> None:
            if self._identity() is None:
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
                    if not self._require_admin():
                        return
                    self._action_add(form)
                elif len(parts) == 4 and parts[:2] == ["actions", "node"]:
                    if not self._may_act_on(parts[2], parts[3]):
                        return
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
            # Read before acting. `login-code` and `login-cancel` serve both
            # cards and the row is what says which — but a cancel deletes that
            # row, so asking afterwards finds nothing and sends you to the wrong
            # one. Asked here, it is still there to answer.
            kind_before = (ctx.store.get_login(node_id) or {}).get("kind")
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
            elif action == "login-start":
                ctx.store.request_login(node_id, form.get("email", ""), time.time())
            elif action == "token-start":
                ctx.store.request_login(node_id, "", time.time(), kind="token")
            elif action == "login-code":
                ctx.store.submit_login_code(node_id, form.get("code", ""), time.time())
            elif action == "login-cancel":
                ctx.store.clear_login(node_id)
            elif action == "token-show":
                # Readable for as long as the attempt lasts, so a second machine
                # can have it without minting another. The expiry bounds how
                # long that is; "token-done" ends it sooner.
                secret = ctx.store.read_secret(node_id)
                node = ctx.store.get_node(node_id) or {}
                self._send(200, render_token_result(node_id, secret, ctx.cfg,
                                                    node.get("owner", "")).encode("utf-8"),
                           HTML_HEADERS)
                return
            elif action == "token-done":
                # Finished with it. Nothing makes the server forget a credential
                # faster than being told it is no longer needed.
                ctx.store.clear_login(node_id)
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
            # A login-code or a cancel can belong to either card, and the row
            # itself knows which: the flow's own kind decides where it is shown.
            anchor = self.ACTION_ANCHORS.get(action, "")
            if anchor == "sign-in" and kind_before == "token":
                anchor = "device-tokens"
            self._redirect(f"/#{anchor}" if anchor else "/")

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
            # An owner now has exactly one thing to submit — their own sign-in —
            # so they get a CSRF token where before they got none. The token is
            # not authorisation: every write still goes through _may_act_on,
            # which refuses an owner any action outside OWNER_ACTIONS and any
            # node that is not theirs. The management forms are still rendered
            # for admins only.
            rows = self._rows(who)
            logins = {r["id"]: ctx.store.get_login(r["id"]) for r in rows}
            return render_dashboard(rows,
                                    self._scope_alerts(ctx.store.open_alerts(), who),
                                    time.time(), ctx.cfg, csrf_token(ctx.cfg), who,
                                    logins=logins)

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
