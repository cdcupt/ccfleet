"""Two sites from one server: the product, and the operator's console.

With an admin hostname configured, each site answers only for its own
audience. The product never shows a console, whatever credentials arrive with
the request; the console is reached on its own hostname, or on loopback for
the operator's tunnel. Operators sign in with Google like everybody else, but
only an account made an admin from the server's own command line gets in.
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import threading
import urllib.parse

import pytest

from ccfleetd import oauth
from ccfleetd.api import Context, build_server, csrf_token, request_host
from ccfleetd.config import Config, ConfigError
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import Store

SECRET = "0123456789abcdef0123456789abcdef"
ADMIN_TOKEN = "admin-token-long-enough-to-pass"
PRODUCT = "fleet.example.com"
ADMIN = "admin.fleet.example.com"


class Browser:
    """A browser pointed at one hostname, with that hostname's cookies."""

    def __init__(self, port, host):
        self.port, self.host = port, host
        self.jar: dict[str, str] = {}

    def call(self, method, path, form=None, headers=None):
        hdrs = {"Host": self.host, **(headers or {})}
        if self.jar and "Cookie" not in hdrs:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.jar.items())
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
            hdrs["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=hdrs)
        reply = conn.getresponse()
        reply.body = reply.read().decode("utf-8", "replace")
        conn.close()
        for header in reply.headers.get_all("Set-Cookie") or []:
            name, _, value = header.split(";")[0].partition("=")
            if value:
                self.jar[name] = value
            else:
                self.jar.pop(name, None)
        return reply

    def sign_in_with_google(self):
        start = self.call("GET", "/auth/google/start?next=/")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(start.getheader("Location")).query)
        back = self.call("GET", f"/auth/google/callback?state={query['state'][0]}&code=c")
        assert back.status == 303, back.body
        return query["redirect_uri"][0]


def _basic(token=ADMIN_TOKEN):
    return {"Authorization": "Basic " + base64.b64encode(f"admin:{token}".encode()).decode()}


def _server(cfg, monkeypatch, who):
    monkeypatch.setattr(oauth, "exchange_code", lambda **kw: "access-token")
    monkeypatch.setattr(oauth, "fetch_identity", lambda token, **kw: dict(who))
    store = Store(":memory:")
    srv = build_server(Context(store, cfg, Monitor(store, cfg, LogNotifier())),
                       host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, store, thread


@pytest.fixture
def split(monkeypatch):
    who = {"sub": "google-erik", "email": "erik@example.com"}
    cfg = Config(bind_host="127.0.0.1", bind_port=0, db_path=":memory:",
                 admin_token=ADMIN_TOKEN, public_url=f"https://{PRODUCT}",
                 google_client_id="cid", google_client_secret="secret",
                 cookie_secret=SECRET, cookie_secure=False, admin_host=ADMIN)
    srv, store, thread = _server(cfg, monkeypatch, who)
    port = srv.server_address[1]
    yield store, (lambda host: Browser(port, host)), who
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)
    store.close()


# -- the hostname ----------------------------------------------------------------

@pytest.mark.parametrize("header,host", [
    ("Admin.Fleet.Example.com", "admin.fleet.example.com"),
    ("admin.fleet.example.com:443", "admin.fleet.example.com"),
    ("127.0.0.1:8111", "127.0.0.1"),
    ("[::1]:8111", "::1"),
    ("[::1]", "::1"),
    ("", ""),
    (None, ""),
])
def test_the_hostname_is_read_without_its_port_or_its_case(header, host):
    assert request_host(header) == host


@pytest.mark.parametrize("raw", ["https://admin.example.com", "admin.example.com:8443",
                                 "admin.example.com/console", "admin", "-admin.example.com",
                                 "admin_x.example.com"])
def test_an_admin_host_that_is_not_a_hostname_is_refused(raw):
    with pytest.raises(ConfigError):
        Config.from_env({"CCFLEET_ADMIN_HOST": raw})


def test_the_admin_callback_is_on_the_admin_host():
    cfg = Config.from_env({"CCFLEET_ADMIN_HOST": "Admin.Example.com",
                           "CCFLEET_PUBLIC_URL": "https://example.com"})
    assert cfg.admin_host == "admin.example.com"
    assert cfg.admin_redirect_uri == "https://admin.example.com/auth/google/callback"
    assert Config.from_env({}).admin_redirect_uri == ""
    # Local development over plain http keeps plain http, or the cookie the
    # sign-in sets would come back to a scheme the browser never sends it to.
    local = Config.from_env({"CCFLEET_ADMIN_HOST": "admin.test",
                             "CCFLEET_PUBLIC_URL": "http://fleet.test"})
    assert local.admin_redirect_uri == "http://admin.test/auth/google/callback"


# -- the product never shows a console ---------------------------------------------

def test_the_products_front_door_sends_each_person_to_their_own_page(split):
    """A visitor to what ccfleet is; somebody signed in to their slots; and an
    operator signed in here too, because on the product an operator is a
    customer like anybody else and there is no console to send them to."""
    store, browser, _ = split
    reply = browser(PRODUCT).call("GET", "/")
    assert reply.status == 303 and reply.getheader("Location") == "/docs"
    person = browser(PRODUCT)
    person.sign_in_with_google()
    assert person.call("GET", "/").getheader("Location") == "/account"
    store.set_account_role(store.account_by_google_sub("google-erik")["id"], "admin")
    assert person.call("GET", "/").getheader("Location") == "/account"
    assert person.call("GET", "/admin").status == 404
    assert browser(PRODUCT).call("GET", "/account").status == 200


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/nodes"), ("GET", "/api/alerts"), ("GET", "/auth/basic"),
    ("POST", "/actions/node/add"), ("POST", "/actions/node/m1/enable"),
])
def test_the_product_has_no_console_even_for_the_operator(split, method, path):
    """Not 401, not 403: there is no console here to be refused from. The
    admin token itself opens nothing on this hostname."""
    store, browser, _ = split
    reply = browser(PRODUCT).call(method, path, form={"csrf": "x"} if method == "POST" else None,
                                  headers=_basic())
    assert reply.status == 404
    assert store.list_nodes() == []


def test_the_privacy_page_is_the_products(split):
    _, browser, _ = split
    assert browser(PRODUCT).call("GET", "/privacy").status == 200
    assert browser(ADMIN).call("GET", "/privacy").status == 404


def test_the_console_side_has_no_user_site(split):
    _, browser, _ = split
    assert browser(ADMIN).call("GET", "/account").status == 404
    assert browser(ADMIN).call("POST", "/account/claim", form={"csrf": "x"}).status == 404


# -- the console's side ---------------------------------------------------------------

def test_the_console_answers_the_operator_on_its_own_hostname(split):
    _, browser, _ = split
    root = browser(ADMIN).call("GET", "/", headers=_basic())
    assert root.status == 303 and root.getheader("Location") == "/admin"
    reply = browser(ADMIN).call("GET", "/admin", headers=_basic())
    assert reply.status == 200 and "ccfleet" in reply.body
    assert browser(ADMIN).call("GET", "/api/nodes", headers=_basic()).status == 200


def test_the_tunnel_still_reaches_the_console(split):
    """Node 1's heartbeats and the operator's break-glass both come in over
    an SSH tunnel to loopback, whatever the public hostnames are."""
    _, browser, _ = split
    for host in ("127.0.0.1:8111", "localhost", "[::1]:8111"):
        assert browser(host).call("GET", "/admin", headers=_basic()).status == 200, host


def test_the_console_door_offers_google_not_a_password_prompt(split):
    _, browser, _ = split
    door = browser(ADMIN).call("GET", "/admin")
    assert door.status == 401
    assert door.getheader("WWW-Authenticate") is None, "the browser would ask for a password"
    assert 'href="/auth/google/start?next=/admin"' in door.body
    assert 'href="/auth/basic"' in door.body


def test_the_admin_token_is_asked_for_only_when_asked(split):
    _, browser, _ = split
    challenge = browser(ADMIN).call("GET", "/auth/basic")
    assert challenge.status == 401 and challenge.getheader("WWW-Authenticate")
    through = browser(ADMIN).call("GET", "/auth/basic", headers=_basic())
    assert through.status == 303 and through.getheader("Location") == "/admin"
    assert browser(ADMIN).call("GET", "/auth/basic", headers=_basic("wrong")).status == 401


def test_heartbeats_are_answered_on_every_hostname(split):
    store, browser, _ = split
    token = store.add_node("n1", "erik")
    body = json.dumps({"node_id": "n1"}).encode()
    for host in (PRODUCT, ADMIN, "127.0.0.1:8111"):
        conn = http.client.HTTPConnection("127.0.0.1", browser(host).port, timeout=10)
        conn.request("POST", "/api/heartbeat", body=body,
                     headers={"Host": host, "Authorization": f"Bearer {token}",
                              "Content-Type": "application/json"})
        reply = conn.getresponse()
        reply.read()
        conn.close()
        assert reply.status == 200, host


# -- operators sign in with Google -----------------------------------------------------

def test_google_sends_each_site_back_to_itself(split):
    """The session is a cookie only the hostname that set it can read, so the
    callback has to land where the sign-in started."""
    _, browser, _ = split
    assert browser(ADMIN).sign_in_with_google() == f"https://{ADMIN}/auth/google/callback"
    assert browser(PRODUCT).sign_in_with_google() == f"https://{PRODUCT}/auth/google/callback"


def test_an_operator_signs_in_with_google(split):
    store, browser, _ = split
    admin = browser(ADMIN)
    admin.sign_in_with_google()
    store.set_account_role(store.account_by_google_sub("google-erik")["id"], "admin")
    console = admin.call("GET", "/admin")
    assert console.status == 200 and "erik@example.com" in console.body
    assert admin.call("GET", "/api/nodes").status == 200


def test_an_operator_session_can_act_with_the_consoles_form_token(split):
    store, browser, _ = split
    admin = browser(ADMIN)
    admin.sign_in_with_google()
    store.set_account_role(store.account_by_google_sub("google-erik")["id"], "admin")
    reply = admin.call("POST", "/actions/node/add",
                       form={"csrf": csrf_token(Config(admin_token=ADMIN_TOKEN)),
                             "node_id": "n9", "owner": "erik"})
    assert reply.status == 200 and store.get_node("n9") is not None


def test_a_signed_in_account_that_is_not_an_operator_gets_no_console(split):
    store, browser, _ = split
    admin = browser(ADMIN)
    admin.sign_in_with_google()
    door = admin.call("GET", "/admin")
    assert door.status == 401 and "not an operator account" in door.body
    assert admin.call("GET", "/api/nodes").status == 401
    reply = admin.call("POST", "/actions/node/add",
                       form={"csrf": csrf_token(Config(admin_token=ADMIN_TOKEN)),
                             "node_id": "n9", "owner": "erik"})
    assert reply.status == 401 and store.get_node("n9") is None


def test_a_session_from_one_site_opens_nothing_on_the_other(split):
    """Two sites, two sessions. A browser never sends one hostname's cookie to
    the other, and the server does not honour one that arrives anyway: an
    operator's product session, carried to the console by hand, is nobody."""
    store, browser, _ = split
    product = browser(PRODUCT)
    product.sign_in_with_google()
    store.set_account_role(store.account_by_google_sub("google-erik")["id"], "admin")
    assert product.call("GET", "/").getheader("Location") == "/account"

    carried = browser(ADMIN)
    carried.jar = dict(product.jar)
    assert carried.call("GET", "/api/nodes").status == 401
    door = carried.call("GET", "/admin")
    assert door.status == 401 and "not an operator" not in door.body, \
        "the product session was read as a signed-in account here"

    admin = browser(ADMIN)
    admin.sign_in_with_google()
    back = browser(PRODUCT)
    back.jar = dict(admin.jar)
    assert "Continue with Google" in back.call("GET", "/account").body, \
        "the console session signed somebody in to the product"


def test_signing_out_lands_on_each_sites_own_front_door(split):
    store, browser, _ = split
    admin = browser(ADMIN)
    admin.sign_in_with_google()
    door = admin.call("GET", "/admin")
    token = re.search(r'name="csrf" value="([0-9a-f]{64})"', door.body).group(1)
    out = admin.call("POST", "/auth/signout", form={"csrf": token})
    assert out.status == 303 and out.getheader("Location") == "/admin"


# -- one hostname, as before ------------------------------------------------------------

def test_with_no_admin_host_one_site_is_both_as_it_always_was(monkeypatch):
    who = {"sub": "google-erik", "email": "erik@example.com"}
    cfg = Config(bind_host="127.0.0.1", bind_port=0, db_path=":memory:",
                 admin_token=ADMIN_TOKEN, public_url=f"https://{PRODUCT}",
                 google_client_id="cid", google_client_secret="secret",
                 cookie_secret=SECRET, cookie_secure=False)
    srv, store, thread = _server(cfg, monkeypatch, who)
    try:
        one = Browser(srv.server_address[1], PRODUCT)
        assert one.call("GET", "/").getheader("Location") == "/docs"
        assert one.call("GET", "/", headers=_basic()).getheader("Location") == "/admin"
        assert one.call("GET", "/admin", headers=_basic()).status == 200
        assert one.call("GET", "/admin/", headers=_basic()).status == 200
        assert one.call("GET", "/account").status == 200
        one.sign_in_with_google()
        assert one.call("GET", "/").getheader("Location") == "/account", \
            "a customer's bare address is their own page"
        assert one.call("GET", "/admin").status == 401
        store.set_account_role(store.account_by_google_sub("google-erik")["id"], "admin")
        assert one.call("GET", "/").getheader("Location") == "/admin", \
            "an operator's bare address is the console"
        assert one.call("GET", "/admin").status == 200, "an operator's session opens the console"
        assert one.call("GET", "/account").status == 200, "and their own slots are still theirs"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
        store.close()


def test_without_google_the_console_asks_for_the_token_as_before(monkeypatch):
    cfg = Config(bind_host="127.0.0.1", bind_port=0, db_path=":memory:",
                 admin_token=ADMIN_TOKEN, admin_host=ADMIN)
    srv, store, thread = _server(cfg, monkeypatch, {})
    try:
        door = Browser(srv.server_address[1], ADMIN).call("GET", "/admin")
        assert door.status == 401 and door.getheader("WWW-Authenticate")
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
        store.close()


# -- making an operator -------------------------------------------------------------------

def test_only_the_servers_command_line_makes_an_operator(tmp_path, capsys):
    from ccfleetd import cli
    db = str(tmp_path / "fleet.db")
    st = Store(db)
    account = st.upsert_account_from_google("g-1", "ops@example.com", now=1.0)
    session = st.create_session(account["id"], now=1.0, ttl_s=3600)
    st.close()

    assert cli.main(["--db", db, "account", "role", "ops@example.com", "admin"]) == 0
    assert "is an operator" in capsys.readouterr().out
    st = Store(db)
    assert st.get_account(account["id"])["role"] == "admin"
    st.close()

    assert cli.main(["--db", db, "account", "role", "ops@example.com", "user"]) == 0
    out = capsys.readouterr().out
    assert "no longer an operator" in out and "ended 1 session" in out
    st = Store(db)
    try:
        assert st.get_account(account["id"])["role"] == "user"
        assert st.account_for_session(session, now=2.0) is None, \
            "an operator taken off kept the console for the rest of their session"
    finally:
        st.close()

    assert cli.main(["--db", db, "account", "role", "nobody@example.com", "admin"]) != 0
    assert "sign in once first" in capsys.readouterr().err


def test_a_role_is_one_of_two_things(store):
    from ccfleetd.store import StoreError
    account = store.upsert_account_from_google("g-1", "a@example.com", now=1.0)
    with pytest.raises(StoreError):
        store.set_account_role(account["id"], "root")
    assert store.set_account_role("nobody", "admin") is False


def test_nothing_on_either_site_changes_a_role(split):
    """Every route a signed-in person can reach, pressed with every role-ish
    field, and nobody becomes an operator."""
    store, browser, _ = split
    product = browser(PRODUCT)
    product.sign_in_with_google()
    token = re.search(r'name="csrf" value="([0-9a-f]{64})"',
                      product.call("GET", "/account").body).group(1)
    for path in ("/account/claim", "/account/role", "/account/admin", "/auth/signout"):
        product.call("POST", path, form={"csrf": token, "role": "admin"})
    assert store.account_by_google_sub("google-erik")["role"] == "user"


def test_a_session_belongs_to_one_of_two_sites(store):
    from ccfleetd.store import StoreError
    account = store.upsert_account_from_google("g-1", "a@example.com", now=1.0)
    with pytest.raises(StoreError):
        store.create_session(account["id"], now=1.0, ttl_s=3600, site="root")
    sid = store.create_session(account["id"], now=1.0, ttl_s=3600, site="admin")
    assert store.account_for_session(sid, now=2.0, site="admin")["id"] == account["id"]
    assert store.account_for_session(sid, now=2.0, site="product") is None



@pytest.mark.parametrize("host", ["127.0.0.1:8111", "localhost", "alias.fleet.example.com"])
def test_google_sign_in_starts_only_where_it_can_finish(split, host):
    """Started on loopback or an alias, Google would send the person back to
    the product's hostname, whose cookie the browser that started never sees.
    Refused, with where to go instead."""
    store, browser, _ = split
    for path in ("/auth/google/start?next=/", "/auth/google/callback?state=s&code=c"):
        reply = browser(host).call("GET", path)
        assert reply.status == 404, (host, path)
        assert f"https://{PRODUCT}" in reply.body and f"https://{ADMIN}" in reply.body
    assert store.list_accounts() == []


def test_with_one_hostname_sign_in_still_starts_only_on_it(monkeypatch):
    cfg = Config(bind_host="127.0.0.1", bind_port=0, db_path=":memory:",
                 admin_token=ADMIN_TOKEN, public_url=f"https://{PRODUCT}",
                 google_client_id="cid", google_client_secret="secret",
                 cookie_secret=SECRET, cookie_secure=False)
    srv, store, thread = _server(cfg, monkeypatch, {"sub": "s", "email": "e@example.com"})
    try:
        port = srv.server_address[1]
        assert Browser(port, "127.0.0.1:8111").call("GET", "/auth/google/start").status == 404
        assert Browser(port, PRODUCT).call("GET", "/auth/google/start").status == 303
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
        store.close()
