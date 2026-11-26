"""HTTP boundary for device enrollment and the signed-in owner's machines."""

import contextlib
import re
import threading
import time
from urllib.parse import urlencode, urlsplit

from . import home_mac_bundle, home_macs, usersite

DOWNLOAD = "/downloads/ccfleet-home-mac.command"
DOWNLOADS = {
    "/downloads/" + name: name
    for name in (home_mac_bundle.COMMAND, home_mac_bundle.MANIFEST, home_mac_bundle.SIGNATURE)
}
DEVICE_ACTIONS = {home_macs.API + name for name in ("begin", "check-in", "disconnect", "wait")}


class StreamBudget:
    """Reserve HTTP capacity for other Fleet routes and enforce a per-Mac limit."""

    def __init__(self, maximum):
        self.maximum = max(1, maximum // 4)
        self.lock = threading.Lock()
        self.active = {}

    @contextlib.contextmanager
    def admit(self, registration):
        with self.lock:
            count = self.active.get(registration, 0)
            allowed = count < 4 and sum(self.active.values()) < self.maximum
            if allowed:
                self.active[registration] = count + 1
        try:
            yield allowed
        finally:
            if allowed:
                with self.lock:
                    self.active[registration] -= 1
                    if self.active[registration] == 0:
                        del self.active[registration]


def enabled(ctx, handler):
    return (
        handler._product_site()
        and not ctx.cfg.broker_only
        and bool(ctx.cfg.home_mac_owners)
        and bool(ctx.cfg.cookie_secret)
    )


def invited(cfg, account):
    return account["id"] in cfg.home_mac_owners or "*" in cfg.home_mac_owners


def render(handler, ctx, account, session, status=200, error=None):
    from .api import HTML_HEADERS
    from .home_mac_ui import page

    viewer = usersite.viewer_for(account, session, ctx.cfg, ctx.store)
    body = page(viewer, home_macs.for_owner(ctx.store, account["id"], time.time()), error=error)
    headers = dict(HTML_HEADERS)
    headers["Content-Security-Policy"] += (
        "; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    handler._send(status, body.encode(), headers)


def get(handler, ctx, path):
    if not enabled(ctx, handler):
        handler._json(404, {"error": "not found"})
        return
    if path in DOWNLOADS:
        # Always bind the connector to the configured public service, never a Host header.
        origin = ctx.cfg.public_url.rstrip("/")
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not origin.isascii()
        ):
            handler._json(503, {"error": "No secure enrollment origin is configured."})
            return
        name = DOWNLOADS[path]
        try:
            content = home_mac_bundle.verified_assets(ctx.cfg.home_mac_release_dir, origin)[name]
        except (OSError, ValueError):
            handler._json(503, {"error": "The signed Mac connector is unavailable."})
            return
        handler._send(
            200,
            content,
            {
                "Content-Type": "application/octet-stream",
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )
        return
    account, session = handler._signed_in()
    if not account:
        if ctx.cfg.google_ready:
            destination = handler._page_location(home_macs.PAGE)
            handler._redirect("/auth/google/start?" + urlencode({"next": destination}))
        else:
            handler._redirect("/account")
    elif not invited(ctx.cfg, account):
        handler._json(404, {"error": "not found"})
    else:
        render(handler, ctx, account, session)


def account_action(handler, ctx, account, session, path, form):
    if not enabled(ctx, handler) or not invited(ctx.cfg, account):
        handler._json(404, {"error": "not found"})
        return
    try:
        if path.endswith("/approve") and set(form) == {"csrf", "code", "name"}:
            home_macs.approve(ctx.store, account["id"], form["code"], form["name"], time.time())
        elif path.endswith("/remove") and set(form) == {"csrf", "node_id"}:
            home_macs.remove(ctx.store, account["id"], form["node_id"], time.time())
        else:
            raise home_macs.invalid()
    except home_macs.Error as exc:
        render(handler, ctx, account, session, exc.status, exc.message)
        return
    handler._redirect(home_macs.PAGE)


def device(handler, ctx, path):
    if not enabled(ctx, handler):
        handler._json(404, {"error": "not found"})
        return
    auth = handler.headers.get_all("Authorization", [])
    match = re.fullmatch(r"Bearer ([0-9a-f]{64})", auth[0]) if len(auth) == 1 else None
    if not match:
        handler._json(401, {"error": "invalid machine credential"})
        return
    if len(handler.headers.get("Content-Length", "")) > 5 or not re.fullmatch(
        r"[0-9]{1,4}", handler.headers.get("Content-Length", "")
    ):
        handler._json(400, {"error": "invalid enrollment form"})
        return
    fields = handler._form(strict=True)
    if fields is None:
        return
    token = match.group(1)
    now = time.time()
    try:
        if path.endswith("/begin"):
            result = home_macs.begin(ctx.store, ctx.cfg.cookie_secret, token, fields, now)
        elif fields:
            raise home_macs.invalid()
        elif path.endswith("/wait"):
            result = wait_for_stream(ctx, token)
        elif path.endswith("/disconnect"):
            home_macs.disconnect(ctx.store, token, now)
            result = {"state": "removed"}
        else:
            result = home_macs.check_in(ctx.store, token, now)
    except home_macs.Error as exc:
        handler._json(exc.status, {"error": exc.message[0]}, {"Cache-Control": "no-store"})
        return
    handler._json(200, result, {"Cache-Control": "no-store"})


def stream(handler, ctx, ticket):
    from . import cli_access

    if not enabled(ctx, handler):
        handler._json(404, {"error": "not found"})
        return
    auth = handler.headers.get_all("Authorization", [])
    match = re.fullmatch(r"Bearer ([0-9a-f]{64})", auth[0]) if len(auth) == 1 else None
    node = home_macs.active(ctx.store, match.group(1), ctx.cfg) if match else None
    if not node:
        handler._json(403, {"error": "Mac unavailable"})
        return
    accept = cli_access.websocket_accept(handler.headers.get("Sec-WebSocket-Key", ""))
    if (
        not accept
        or handler.headers.get("Upgrade", "").lower() != "websocket"
        or "upgrade"
        not in {s.strip().lower() for s in handler.headers.get("Connection", "").split(",")}
        or handler.headers.get("Sec-WebSocket-Version") != "13"
    ):
        handler._json(400, {"error": "invalid stream upgrade"})
        return
    with ctx.home_mac_streams.admit(node["registration_id"]) as allowed:
        if not allowed:
            handler._json(429, {"error": "Mac stream capacity reached"})
            return
        relay_stream(handler, ctx, node, ticket, accept, home_macs.digest(match.group(1)))


def relay_stream(handler, ctx, node, ticket, accept, device_hash):
    from . import cli_access, home_mac_transport

    try:
        peer = home_mac_transport.accept(
            ctx.store, ctx.cfg.db_path, node["registration_id"], ticket
        )
    except OSError:
        peer = None
    if peer is None:
        handler._json(404, {"error": "stream expired"})
        return
    with peer:
        protocol = handler.protocol_version
        try:
            handler.protocol_version = "HTTP/1.1"
            handler.send_response(101)
            handler.send_header("Upgrade", "websocket")
            handler.send_header("Connection", "Upgrade")
            handler.send_header("Sec-WebSocket-Accept", accept)
            handler.end_headers()
            handler.wfile.flush()
        finally:
            handler.protocol_version = protocol
        handler.close_connection = True
        handler.connection.settimeout(None)
        cli_access.relay_websocket(
            handler.connection,
            peer,
            lambda: home_macs.active(ctx.store, device_hash, ctx.cfg, hashed=True) == node,
        )


def wait_for_stream(ctx, token):
    from . import home_mac_transport

    node = home_macs.active(ctx.store, token, ctx.cfg)
    if not node:
        raise home_macs.denied()
    # Idle Macs release the HTTP worker immediately.
    return {"ticket": home_mac_transport.take(ctx.store, node["registration_id"], time.time())}
