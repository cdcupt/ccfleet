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
from typing import Any, Callable, Optional

from . import consoleslots, customer_docs, oauth, sessions, statuspage, usersite
from . import slots as slotstates
from .config import Config
from .desired import desired_state, machine_hostname
from .heartbeat import HeartbeatError, validate_heartbeat
from .monitor import Monitor
from .passwords import verify_password
from .render import (
    CONSOLE_PATH,
    LOCAL_TIMES_CSP,
    build_rows,
    render_add_result,
    render_dashboard,
    render_token_result,
)
from .store import Store, StoreError, slot_login_key

log = logging.getLogger("ccfleetd.api")

NODE_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
# The operator's way in from the box itself — the SSH tunnel, or a shell on
# it — is the console's side whatever the admin hostname is.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
HTML_HEADERS = {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # Images: data: only, which is the icon in the tab; default-src 'none'
    # blocked it on every page. Scripts: the one that says reset times in the
    # viewer's zone, by its hash.
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; "
                                f"img-src data:; script-src {LOCAL_TIMES_CSP}"),
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


def request_host(header: Optional[str]) -> str:
    """The hostname a request was made to: lowercased, without its port."""
    host = (header or "").strip().lower()
    if host.startswith("["):                 # an IPv6 literal, [::1]:8111
        end = host.find("]")
        return host[1:end] if end > 0 else ""
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


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
            who = (identify(self.headers.get("Authorization"), ctx.cfg, ctx.store)
                   or self._operator_session())
            if who is not None:
                return who
            self._json(401, {"error": "unauthorized"},
                       {"WWW-Authenticate": 'Basic realm="ccfleet", charset="UTF-8"'})
            return None

        # -- which site this is --------------------------------------------

        def _admin_site(self) -> bool:
            """Is this request on the console's side?

            With no admin host configured there is one site and it is both,
            as it always was. With one, only that hostname and loopback are:
            every other hostname is the product, and never shows a console,
            whatever credentials arrive with the request.
            """
            if not ctx.cfg.admin_host:
                return True
            host = request_host(self.headers.get("Host"))
            return host == ctx.cfg.admin_host or host in LOOPBACK_HOSTS

        def _product_site(self) -> bool:
            return not ctx.cfg.admin_host or not self._admin_site()

        def _redirect_uri(self) -> str:
            """Where Google sends a sign-in started here back to, or "" when
            no sign-in can start on this hostname.

            It has to be the hostname the sign-in left from: the session is a
            cookie only that hostname can set, and a session is bound to its
            site. So only the two canonical hostnames sign anybody in — the
            admin host, and the one the product's callback names. Loopback, or
            any other alias pointing here, would send the person back to a host
            whose cookie the browser that started never sees.
            """
            host = request_host(self.headers.get("Host"))
            if ctx.cfg.admin_host and host == ctx.cfg.admin_host:
                return ctx.cfg.admin_redirect_uri
            product = ctx.cfg.redirect_uri
            if product and host == request_host(urllib.parse.urlsplit(product).netloc):
                return product
            return ""

        def _operator_session(self) -> Optional[Identity]:
            """An operator signed in with Google, on the console's side.

            Only an account the operator made an admin, from the server's own
            command line: nothing on either site can grant it. The admin token
            still works beside this, as the way in when Google is the thing
            that is down. Whether this is the console's side at all is settled
            before anything asks: the product answers the console's routes
            with 404 first, and a product session is no session here anyway.
            """
            account, _ = self._signed_in()
            if account is None or account.get("role") != "admin":
                return None
            return Identity("admin", "", str(account.get("email") or "operator"))

        def _console_door(self) -> None:
            """The console's answer to somebody not signed in.

            With Google sign-in configured it is a page, not a password prompt:
            operators sign in the way everybody else does, and the admin token
            is one link away for when that is not possible. Without Google it
            is the prompt it always was.
            """
            if not ctx.cfg.google_ready:
                self._json(401, {"error": "unauthorized"},
                           {"WWW-Authenticate": 'Basic realm="ccfleet", charset="UTF-8"'})
                return
            account, session_id = self._signed_in()
            self._send(401, usersite.console_door(account, session_id, ctx.cfg,
                                                  ctx.store).encode("utf-8"), HTML_HEADERS)

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

        def _signed_in(self) -> tuple[Optional[dict[str, Any]], str]:
            """The account this browser is signed in as, and its session id.

            (None, "") when there is none. Separate from _identity(): that is
            the operator's basic auth, which is the console's own door. These
            are the people who rent slots, and the two must not be able to
            stand in for one another.
            """
            session_id = self._session_cookie()
            if not session_id:
                return None, ""
            account = ctx.store.account_for_session(session_id, now=time.time(),
                                                    site=self._site())
            return (account, session_id) if account is not None else (None, "")

        def _session_cookie(self) -> str:
            """The session id this request's cookie carries, verified; "" if none."""
            if not ctx.cfg.cookie_secret:
                return ""
            raw = sessions.read_cookie(self.headers.get("Cookie"))
            if raw is None:
                return ""
            try:
                return sessions.unsign(raw, ctx.cfg.cookie_secret)
            except sessions.SessionError:
                return ""

        def _peek_signed_in(self) -> tuple[Optional[dict[str, Any]], str]:
            """Who is signed in, for a page that only wants to say who is looking.

            Read-only: no visit is recorded and no expired session is swept, so
            reading a public page writes nothing. Anything that acts on the
            session goes through _signed_in() instead.
            """
            session_id = self._session_cookie()
            if not session_id:
                return None, ""
            account = ctx.store.peek_session(session_id, now=time.time(), site=self._site())
            return (account, session_id) if account is not None else (None, "")

        def _viewer(self) -> Optional[usersite.Viewer]:
            """Who is looking at a public page, as its corner shows them."""
            account, session_id = self._peek_signed_in()
            return usersite.viewer_for(account, session_id, ctx.cfg, ctx.store)

        def _docs_page(self, page: Callable[..., str]) -> None:
            """A public page of the product, for whoever is looking, with the
            price the operator set, if any. The front page also says at its
            top whether the service is up, as /status does; only it reads
            the machines for that."""
            current = ctx.store.get_price()
            extra = ({"health": statuspage.health(ctx.store, time.time())}
                     if page is customer_docs.overview else {})
            self._send(200, page(ctx.cfg, viewer=self._viewer(),
                                 price=current["price"] if current else None,
                                 **extra).encode("utf-8"),
                       HTML_HEADERS)

        def _console_corner(self) -> str:
            """The console's corner: whoever is signed in with Google on this
            site, as their menu. The admin token and console passwords are basic
            auth, with no session to show or end: the line under the console's
            title already says who they are."""
            account, session_id = self._peek_signed_in()
            viewer = usersite.viewer_for(account, session_id, ctx.cfg, ctx.store,
                                         on_console=True)
            return viewer.menu() if viewer is not None else ""

        def _site(self) -> str:
            """Which site's sessions this request may use. With one hostname
            there is one site; with two, the operator's hostname is the admin
            site and everything else is the product."""
            if ctx.cfg.admin_host and not self._product_site():
                return "admin"
            return "product"

        def _csrf_ok(self, form: dict[str, str], session_id: str) -> bool:
            return hmac.compare_digest(
                form.get("csrf", ""), usersite.csrf_for(session_id, ctx.cfg.cookie_secret))

        def _sign_in_start(self) -> None:
            if not ctx.cfg.google_ready:
                self._json(503, {"error": "sign-in is not configured"})
                return
            if not self._redirect_uri():
                self._not_a_sign_in_host()
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
                                    redirect_uri=self._redirect_uri(),
                                    state=state, verifier=verifier),
                [sessions.cookie_header(
                    sessions.sign(state, ctx.cfg.cookie_secret),
                    ttl_s=oauth.FLOW_TTL_S, secure=ctx.cfg.cookie_secure,
                    name=sessions.FLOW_COOKIE_NAME)])

        def _sign_in_callback(self) -> None:
            if not ctx.cfg.google_ready:
                self._json(503, {"error": "sign-in is not configured"})
                return
            if not self._redirect_uri():
                self._not_a_sign_in_host()
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
                    redirect_uri=self._redirect_uri())
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
                account["id"], now=time.time(), ttl_s=ctx.cfg.session_ttl_s,
                site=self._site())
            self._redirect_with_cookies(
                flow["next_url"] or "/",
                [sessions.cookie_header(
                    sessions.sign(session_id, ctx.cfg.cookie_secret),
                    ttl_s=ctx.cfg.session_ttl_s, secure=ctx.cfg.cookie_secure),
                 self._clear_flow_cookie()])

        def _not_a_sign_in_host(self) -> None:
            """Refused, and pointed at where signing in does work."""
            where = [u.rsplit("/auth/", 1)[0] for u in (ctx.cfg.redirect_uri,
                                                        ctx.cfg.admin_redirect_uri) if u]
            self._json(404, {"error": "sign in at " + " or ".join(where)})

        def _sign_out(self) -> None:
            """End this browser's session: only when the request proves it
            came from our own page.

            SameSite keeps the cookie off a cross-site POST, but not the
            response's Set-Cookie, so another site's form could still clear it
            — signing people out from anywhere. So a live session is ended
            only with its own form token, and a cookie is cleared only when
            one was actually sent with the request.
            """
            home = "/account" if self._product_site() else CONSOLE_PATH
            if sessions.read_cookie(self.headers.get("Cookie")) is None:
                self._redirect(home)
                return
            account, session_id = self._signed_in()
            if account is not None:
                form = self._form()
                if form is None:
                    return
                if not self._csrf_ok(form, session_id):
                    self._json(403, {"error": "bad or missing csrf token"})
                    return
                ctx.store.end_session(session_id)
            # A cookie that no longer names a session is cleared as it is.
            self._redirect_with_cookies(
                home, [sessions.clearing_header(secure=ctx.cfg.cookie_secure)])

        def _account_action(self, path: str) -> None:
            """A form on the user site. Signed in, with this session's token."""
            account, session_id = self._signed_in()
            if account is None:
                # Their session ended while the page sat open. Back to the
                # page, which offers sign-in, rather than an error.
                self._redirect("/account")
                return
            form = self._form()
            if form is None:
                return
            if not self._csrf_ok(form, session_id):
                self._json(403, {"error": "bad or missing csrf token"})
                return
            outcome = usersite.act(ctx.store, ctx.cfg, account, path, form, time.time(),
                                   session_id)
            if outcome.location:
                self._redirect(outcome.location)
            else:
                self._send(outcome.status, outcome.body.encode("utf-8"), HTML_HEADERS)

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
            admin_only = (path in ("/api/nodes", "/api/alerts", "/auth/basic")
                          or path.rstrip("/") == CONSOLE_PATH)
            if path == "/healthz":
                self._json(200, {"ok": True})
            elif (admin_only and not self._admin_site()) or (
                    (path in ("/account", "/privacy", "/status")
                     or customer_docs.page_for(path))
                    and not self._product_site()):
                # Each site answers only for its own audience: the product
                # never shows a console, the console never plays product.
                self._json(404, {"error": "not found"})
            elif path == "/auth/basic":
                # The admin token, asked for deliberately: the break-glass way
                # in beside Google, from the link on the console's door.
                if identify(self.headers.get("Authorization"), ctx.cfg, ctx.store):
                    self._redirect(CONSOLE_PATH)
                else:
                    self._json(401, {"error": "unauthorized"},
                               {"WWW-Authenticate": 'Basic realm="ccfleet", charset="UTF-8"'})
            elif path == "/account":
                account, session_id = self._signed_in()
                note = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query).get("note", [""])[0]
                self._send(200, usersite.page(ctx.store, ctx.cfg, account, session_id,
                                              time.time(), note).encode("utf-8"),
                           HTML_HEADERS)
            elif path == "/privacy":
                # Public: Google links here from its sign-in screen.
                self._send(200, usersite.privacy_page(ctx.cfg, self._viewer()).encode("utf-8"),
                           HTML_HEADERS)
            elif path == "/status":
                # Public: whether the site and the machines are up, never who is on them.
                self._send(200, statuspage.page(ctx.store, ctx.cfg, self._viewer(),
                                                time.time()).encode("utf-8"), HTML_HEADERS)
            elif customer_docs.page_for(path):
                # Public, for people deciding whether to buy a slot and then using one.
                self._docs_page(customer_docs.page_for(path))
            elif path == "/auth/google/start":
                self._sign_in_start()
            elif path == "/auth/google/callback":
                self._sign_in_callback()
            elif path == "/":
                # The bare address is the product's front page, the same page
                # for everybody: what ccfleet is, with a way on that suits
                # whoever is looking. Only the console's side of two sites (its
                # own hostname, or loopback for the operator's tunnel) keeps it
                # for the console, since the console is all that side serves.
                if not self._product_site():
                    self._redirect(CONSOLE_PATH)
                    return
                self._docs_page(customer_docs.overview)
            elif path.rstrip("/") == CONSOLE_PATH:
                who = (identify(self.headers.get("Authorization"), ctx.cfg, ctx.store)
                       or self._operator_session())
                if who is None:
                    self._console_door()
                else:
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
            if path.startswith("/account/"):
                if not self._product_site():
                    self._json(404, {"error": "not found"})
                    return
                self._account_action(path)
                return
            if path.startswith("/actions/"):
                if not self._admin_site():
                    self._json(404, {"error": "not found"})
                    return
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
            self._record_login((payload.get("reconcile") or {}).get("login"),
                               node["id"], now)
            # Before the alerts are evaluated and before the reply is built, so
            # both see the slots as this report left them: a wipe confirmed
            # here is a free slot in the same reply, not one beat later.
            if "slots" in payload:
                ctx.store.apply_slot_report(node["id"], payload["slots"], now=now)
                for report in payload["slots"]:
                    # Looked up on this machine only: a machine can move the
                    # sign-in of its own slots and nobody else's.
                    slot = ctx.store.slot_on_machine(node["id"], report["unix_user"])
                    if slot is not None:
                        self._record_login(report.get("login"),
                                           slot_login_key(slot["id"]), now)
                        self._record_update(report.get("claude_update"), slot["id"], now)
            # Somebody's own node, counted as their slot, says what it did about
            # an update asked for on their page under its own reconcile block.
            owned = ctx.store.list_slots(node_id=node["id"], kind=slotstates.OWNER_SLOT)
            for own in owned:
                self._record_update((payload.get("reconcile") or {}).get("claude_update"),
                                    own["id"], now)
            events = ctx.monitor.record_heartbeat(node, payload, now)
            # A machine's own slots only. An owner's node counted as their slot
            # is a record of ours: its agent is never told to provision it.
            slots = ctx.store.list_slots(node_id=node["id"], kind=slotstates.MACHINE_SLOT)
            # A shared machine answers to its slot's name; an owner's node is
            # never told what to call itself.
            hostname = (machine_hostname(node["id"], slots)
                        if payload.get("mode") == slotstates.MACHINE_MODE else None)
            self._json(200, {
                "ok": True,
                # Kept for agents predating the desired block; same value, new home.
                "pinned_version": node["pinned_version"],
                "desired": desired_state(
                    node, ctx.store.get_login(node["id"]), slots,
                    {s["id"]: ctx.store.get_login(slot_login_key(s["id"])) for s in slots},
                    hostname=hostname, channels=ctx.store.get_channel_versions(),
                    slot_updates={s["id"]: ctx.store.get_claude_update(s["id"]) for s in slots},
                    own_update=ctx.store.get_claude_update(owned[0]["id"]) if owned else None),
                "open_alerts": [a["rule"] for a in ctx.store.open_alerts(node["id"])],
                "events": len(events)})

        def _record_update(self, update: Any, slot_id: str, now: float) -> None:
            """What a node says it did about an update asked for on a page. The
            store only lets it close the request it names, if still waiting."""
            if not isinstance(update, dict):
                return
            ctx.store.record_claude_update(
                slot_id, update.get("requested_at"), str(update.get("state") or ""),
                str(update.get("to") or ""), str(update.get("detail") or ""), now)

        def _record_login(self, login: Any, key: str, now: float) -> None:
            """What a node says about a sign-in, filed against `key`: the node
            itself, or one of its slots."""
            if not isinstance(login, dict) or not login.get("state"):
                return
            requested_at = login.get("requested_at")
            ctx.store.record_login_progress(
                key, str(login.get("state")), str(login.get("url") or ""),
                str(login.get("detail") or ""), now,
                requested_at if isinstance(requested_at, (int, float))
                and not isinstance(requested_at, bool) else None,
                secret=str(login.get("secret") or ""))
            # Consumed. The store redacts it before anything is archived,
            # which is the guarantee; dropping it here as well keeps a
            # credential from travelling further through this process than
            # the one call that needed it.
            login.pop("secret", None)

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
            who = self._identity()
            if who is None:
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
                elif len(parts) == 4 and parts[1] in ("machine", "slot", "account",
                                                      "payment", "price"):
                    # Slots and the people who hold them are the operator's alone.
                    if not self._require_admin():
                        return
                    anchor = consoleslots.act(ctx.store, parts[1], parts[2], parts[3],
                                              form, time.time(), by=who.label)
                    if anchor is None:
                        self._json(404, {"error": "not found"})
                    else:
                        self._redirect(f"{CONSOLE_PATH}#{anchor}")
                else:
                    self._json(404, {"error": "not found"})
            except StoreError as exc:
                page = (f'<!doctype html><p>{html_escape(str(exc))}</p>'
                        f'<p><a href="{CONSOLE_PATH}">back</a></p>')
                self._send(400, page.encode("utf-8"), HTML_HEADERS)

        def _action_add(self, form: dict[str, str]) -> None:
            token = ctx.store.add_node(
                form.get("node_id", "").strip(), form.get("owner", "").strip(),
                form.get("region", "").strip(), form.get("pinned_version", "").strip(),
                form.get("rc_expected") == "1", now=time.time())
            body = render_add_result(form["node_id"].strip(), token, ctx.cfg,
                                     form.get("owner", "").strip(), self._console_corner())
            self._send(200, body.encode("utf-8"), HTML_HEADERS)

        def _shared_machine(self, node_id: str) -> bool:
            """A node with a machine slot, or whose agent says it is a shared machine."""
            if ctx.store.list_slots(node_id=node_id, kind=slotstates.MACHINE_SLOT):
                return True
            beat = ctx.store.latest_heartbeats().get(node_id) or {}
            return (beat.get("payload") or {}).get("mode") == slotstates.MACHINE_MODE

        def _action_on_node(self, node_id: str, action: str, form: dict[str, str]) -> None:
            # Read before acting. `login-code` and `login-cancel` serve both
            # cards and the row is what says which — but a cancel deletes that
            # row, so asking afterwards finds nothing and sends you to the wrong
            # one. Asked here, it is still there to answer.
            kind_before = (ctx.store.get_login(node_id) or {}).get("kind")
            if action in ("login-start", "token-start") and self._shared_machine(node_id):
                # Root on a shared machine runs no Claude Code, and its agent
                # only signs in slots, so this would sit unanswered for fifteen
                # minutes and then fail. A slot's sign-in and tokens are its
                # holder's, from their own page.
                raise StoreError(f"{node_id} is a shared machine: whoever holds its slot "
                                 "signs it in, and gets device tokens, on their own page")
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
                                                    node.get("owner", ""),
                                                    self._console_corner()).encode("utf-8"),
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
                                                  node.get("owner", ""),
                                                  self._console_corner()).encode("utf-8"),
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
            self._redirect(f"{CONSOLE_PATH}#{anchor}" if anchor else CONSOLE_PATH)

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
                              ctx.store.open_alerts(), time.time(),
                              slots=ctx.store.list_slots())

        def _dashboard(self, who: Identity) -> str:
            # An owner now has exactly one thing to submit — their own sign-in —
            # so they get a CSRF token where before they got none. The token is
            # not authorisation: every write still goes through _may_act_on,
            # which refuses an owner any action outside OWNER_ACTIONS and any
            # node that is not theirs. The management forms are still rendered
            # for admins only.
            rows = self._rows(who)
            logins = {r["id"]: ctx.store.get_login(r["id"]) for r in rows}
            now = time.time()
            return render_dashboard(rows,
                                    self._scope_alerts(ctx.store.open_alerts(), who),
                                    now, ctx.cfg, csrf_token(ctx.cfg), who,
                                    logins=logins,
                                    extra=(consoleslots.section(ctx.store, csrf_token(ctx.cfg),
                                                                now)
                                           if who.is_admin else ""),
                                    corner=self._console_corner())

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
