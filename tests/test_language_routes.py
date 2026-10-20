"""Separate UI language URLs retain the existing account and API boundaries."""

from __future__ import annotations

import http.client
import re
import threading
import urllib.parse

import pytest

from ccfleetd import oauth, reference_docs
from ccfleetd.api import Context, access_log_route, build_server
from ccfleetd.config import Config
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import Store
from tests.test_admin_host import Browser

PRODUCT = "fleet.example.com"
ADMIN = "admin.fleet.example.com"


@pytest.fixture
def language_site(monkeypatch):
    monkeypatch.setattr(oauth, "exchange_code", lambda **kw: "access-token")
    monkeypatch.setattr(oauth, "fetch_identity", lambda token, **kw: {
        "sub": "language-reader", "email": "reader@example.com"})
    cfg = Config(bind_host="127.0.0.1", bind_port=0, db_path=":memory:",
                 admin_token="operator-token-long-enough", public_url=f"https://{PRODUCT}",
                 admin_host=ADMIN, google_client_id="client", google_client_secret="secret",
                 cookie_secret="0123456789abcdef0123456789abcdef", cookie_secure=False)
    store = Store(":memory:")
    server = build_server(Context(store, cfg, Monitor(store, cfg, LogNotifier())),
                          host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield store, server, lambda host=PRODUCT: Browser(server.server_address[1], host)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    store.close()


def _sign_in(browser, next_url="/zh/account", error=""):
    start = browser.call("GET", "/auth/google/start?" + urllib.parse.urlencode({"next": next_url}))
    assert start.status == 303
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(start.getheader("Location")).query)
    callback = "/auth/google/callback?" + urllib.parse.urlencode({
        "state": params["state"][0], "code": "code", **({"error": error} if error else {})})
    return browser.call("GET", callback), params


def _csrf(body):
    return re.search(r'name="csrf" value="([0-9a-f]{64})"', body).group(1)


def test_root_is_english_without_browser_language_state(language_site):
    _, _, make_browser = language_site
    reply = make_browser().call("GET", "/")
    assert reply.status == 200
    assert 'lang="en"' in reply.body
    assert "Local Claude Code, your own account on your slot" in reply.body
    assert "Claude Code 在本机运行" not in reply.body
    assert 'data-language-choice="both"' not in reply.body
    assert "localStorage" not in reply.body
    assert 'href="/zh/"' in reply.body


@pytest.mark.parametrize("path", ["/", "/account", "/docs/guide", "/privacy", "/status"])
def test_language_pages_deliver_only_the_selected_copy_without_javascript(language_site, path):
    _, _, make_browser = language_site
    browser = make_browser()
    en = browser.call("GET", "/en" + path)
    zh = browser.call("GET", "/zh" + path)
    assert en.status == zh.status == 200
    assert 'lang="en"' in en.body and 'lang="zh-Hans"' in zh.body
    assert 'class="copy-zh"' not in en.body
    assert 'class="copy-en"' not in zh.body
    assert 'data-language-choice="both"' not in en.body + zh.body
    assert 'href="/docs/guide"' in en.body
    assert 'href="/zh/docs/guide"' in zh.body


@pytest.mark.parametrize("choice,destination", [
    ("zh", "/zh/docs/guide?note=welcome"),
    ("en", "/en/docs/guide?note=welcome"),
    ("both", "/en/docs/guide?note=welcome"),
])
def test_old_language_queries_redirect_to_distinct_pages(language_site, choice, destination):
    _, _, make_browser = language_site
    reply = make_browser().call("GET", f"/docs/guide?lang={choice}&note=welcome")
    assert reply.status == 303 and reply.getheader("Location") == destination


def test_oauth_keeps_callback_uri_and_returns_to_selected_language(language_site):
    _, _, make_browser = language_site
    browser = make_browser()
    door = browser.call("GET", "/zh/account")
    assert "next=/zh/account" in door.body or "next=%2Fzh%2Faccount" in door.body
    signed_in, params = _sign_in(browser)
    assert params["redirect_uri"] == [f"https://{PRODUCT}/auth/google/callback"]
    assert signed_in.status == 303 and signed_in.getheader("Location") == "/zh/account"
    assert "Path=/" in signed_in.getheader("Set-Cookie")


def test_cancelled_oauth_returns_to_chinese_home(language_site):
    _, _, make_browser = language_site
    cancelled, _ = _sign_in(make_browser(), error="access_denied")
    assert cancelled.status == 303
    assert cancelled.getheader("Location") == "/zh/?signin=cancelled"


def test_chinese_forms_keep_csrf_and_redirect_language(language_site):
    store, _, make_browser = language_site
    browser = make_browser()
    _sign_in(browser)
    account = store.account_by_google_sub("language-reader")
    page = browser.call("GET", "/zh/account")
    token = _csrf(page.body)
    assert 'action="/zh/auth/signout"' in page.body
    refused = browser.call("POST", "/zh/auth/signout", form={"csrf": "0" * 64})
    assert refused.status == 403
    assert browser.call("GET", "/zh/account").status == 200
    # No spare slot: the existing account action returns its normal note.
    claim = browser.call("POST", "/zh/account/claim", form={"csrf": token})
    assert claim.status == 303 and claim.getheader("Location").startswith("/zh/account")
    assert store.account_by_google_sub("language-reader")["id"] == account["id"]
    out = browser.call("POST", "/zh/auth/signout", form={"csrf": token})
    assert out.status == 303 and out.getheader("Location") == "/zh/account"


def test_account_error_page_uses_selected_language_without_disclosing_slot(language_site):
    _, _, make_browser = language_site
    browser = make_browser()
    _sign_in(browser)
    page = browser.call("GET", "/zh/account")
    result = browser.call("POST", "/zh/account/slots/private-id/cli-revoke",
                          form={"csrf": _csrf(page.body)})
    assert result.status == 404
    assert "页面不存在" in result.body and "There is nothing here" not in result.body
    assert "private-id" not in result.body
    assert 'href="/zh/account"' in result.body


def test_head_length_matches_projected_page(language_site):
    _, _, make_browser = language_site
    browser = make_browser()
    get = browser.call("GET", "/zh/docs/guide")
    head = browser.call("HEAD", "/zh/docs/guide")
    assert head.status == 200 and head.body == ""
    assert int(head.getheader("Content-Length")) == len(get.body.encode("utf-8"))


def test_handler_resets_language_between_keepalive_requests(language_site):
    _, server, _ = language_site
    server.RequestHandlerClass.protocol_version = "HTTP/1.1"
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        conn.request("GET", "/zh/", headers={"Host": PRODUCT})
        first = conn.getresponse()
        first_body = first.read().decode()
        assert first.status == 200 and 'lang="zh-Hans"' in first_body
        conn.request("GET", "/", headers={"Host": PRODUCT})
        second = conn.getresponse()
        assert second.status == 200 and 'lang="en"' in second.read().decode()
    finally:
        conn.close()


@pytest.mark.parametrize("method,path", [
    ("GET", "/en/api/nodes"),
    ("POST", "/zh/v1/messages"), ("POST", "/en/api/cli/gateway"),
    ("GET", "/zh/auth/google/callback?state=x&code=x"),
    ("GET", "/zh/docs/library/docs/../README.html"),
    ("GET", "/zh/docs/library/docs/%2e%2e/README.html"),
    ("GET", "/zh/docs/library/docs/runbooks.html"),
    ("GET", "/zh//account"), ("GET", "/zh/../account"),
    ("GET", "https://host.invalid/zh/account"),
])
def test_language_aliases_do_not_expand_api_or_file_authority(language_site, method, path):
    _, _, make_browser = language_site
    assert make_browser().call(method, path).status == 404


@pytest.mark.parametrize("path", ["/en/", "/zh/account", "/zh/docs/guide"])
def test_language_aliases_belong_only_to_product_host(language_site, path):
    _, _, make_browser = language_site
    assert make_browser(ADMIN).call("GET", path).status == 404


def test_static_current_guide_is_projected_but_historical_file_is_unchanged(language_site,
                                                                         monkeypatch):
    _, _, make_browser = language_site
    document = ('<!doctype html><html lang="en"><body data-language="both">'
                '<span class="copy-en">ENGLISH CURRENT</span>'
                '<span class="copy-zh">当前中文</span>'
                '<a href="platform-comparison.html#now">Comparison</a></body></html>').encode()
    monkeypatch.setattr(reference_docs, "load_document", lambda name: document)
    browser = make_browser()
    current = browser.call("GET", "/zh/docs/library/docs/public-gateway.html")
    assert current.status == 200 and "当前中文" in current.body
    assert "ENGLISH CURRENT" not in current.body
    assert 'href="/zh/docs/library/docs/platform-comparison.html#now"' in current.body
    historical = browser.call("GET", "/docs/library/docs/runbooks.html")
    assert historical.status == 200 and historical.body.encode() == document


def test_language_access_logs_keep_fixed_templates():
    assert access_log_route("/zh/docs/guide?note=private") == "/docs/guide"
    assert access_log_route("/zh/account/slots/secret-slot/cli-revoke") == (
        "/account/slots/{slot_id}/cli-revoke")
    assert access_log_route("/zh/private-customer-value") == "unknown_route"
