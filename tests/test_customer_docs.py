"""The pages a customer is sent.

Public on the product, absent from the console's hostname, honest about the
one thing ccfleet does not sell (Claude itself), and never naming a machine's
address. The guide quotes the user site's labels, so it is checked against
them: a guide that tells somebody to press a button that is not there is
worse than no guide.
"""

from __future__ import annotations

import http.client
import inspect
import re
import threading

import pytest

from ccfleetd import customer_docs, usersite
from ccfleetd.api import Context, build_server
from ccfleetd.config import Config
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import Store

PRODUCT = "fleet.example.com"
ADMIN = "admin.fleet.example.com"
DOC_PATHS = ("/docs", "/docs/guide", "/docs/how-it-works", "/docs/terms")


def _serve(**settings):
    cfg = Config(bind_host="127.0.0.1", bind_port=0, db_path=":memory:",
                 admin_token="admin-token-long-enough", public_url=f"https://{PRODUCT}",
                 google_client_id="cid", google_client_secret="secret",
                 cookie_secret="0123456789abcdef0123456789abcdef", cookie_secure=False,
                 **settings)
    store = Store(":memory:")
    srv = build_server(Context(store, cfg, Monitor(store, cfg, LogNotifier())),
                       host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    def get(path, host=PRODUCT):
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        conn.request("GET", path, headers={"Host": host})
        reply = conn.getresponse()
        body = reply.read().decode("utf-8", "replace")
        conn.close()
        return reply.status, body

    def stop():
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
        store.close()
    return get, stop


@pytest.fixture
def one_site():
    get, stop = _serve()
    yield get
    stop()


@pytest.fixture
def two_sites():
    get, stop = _serve(admin_host=ADMIN)
    yield get
    stop()


# -- who can read them ---------------------------------------------------------------

@pytest.mark.parametrize("path", [*DOC_PATHS, "/docs/", "/docs/guide/"])
def test_every_page_is_public(one_site, path):
    status, body = one_site(path)
    assert status == 200 and '<nav class="doc-nav">' in body


def test_a_page_that_does_not_exist_is_not_found(one_site):
    assert one_site("/docs/nope")[0] == 404
    assert one_site("/docsguide")[0] == 404


@pytest.mark.parametrize("path", DOC_PATHS)
def test_with_two_sites_the_pages_belong_to_the_product(two_sites, path):
    assert two_sites(path, host=PRODUCT)[0] == 200
    assert two_sites(path, host=ADMIN)[0] == 404


# -- what they say -------------------------------------------------------------------

def render(path, **settings):
    return customer_docs.page_for(path)(Config(**settings))


@pytest.mark.parametrize("path", DOC_PATHS)
def test_each_page_marks_itself_and_links_the_rest(path):
    page = render(path)
    assert page.count(' class="here"') == 1
    assert f'<a href="{path}" class="here">' in page
    for target, _ in customer_docs.PAGES_TITLES:
        assert f'href="{target}"' in page
    assert 'href="/account"' in page


def test_the_plan_requirement_is_said_where_people_decide():
    """ccfleet sells a machine, not Claude. The overview says so before anybody
    pays, and the terms say it again."""
    overview = render("/docs")
    assert "Pro, Max, Team or Enterprise" in overview
    assert "API keys don&#x27;t work" in overview
    assert "ccfleet sells the machine, not Claude" in overview
    assert "does not provide access to Claude" in render("/docs/terms")


def test_signing_in_comes_before_buying():
    """The operator can only switch on an account that exists, and an account
    exists once its owner has signed in."""
    guide = render("/docs/guide")
    assert guide.index("<h3>Sign in</h3>") < guide.index("<h3>Get a slot</h3>")
    assert "sign in once" in render("/docs")


def test_no_page_names_a_machines_address():
    for path in DOC_PATHS:
        for page in (render(path), render(path, contact_email="help@example.com")):
            assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", page), path


def test_the_operators_address_appears_only_once_they_chose_one():
    for path in ("/docs", "/docs/guide", "/docs/terms"):
        unset = render(path)
        assert "the person who sent you this page" in unset and "mailto:" not in unset
        chosen = render(path, contact_email="sales&co@example.com")
        assert 'href="mailto:sales&amp;co@example.com"' in chosen
        assert "sales&co@example.com" not in chosen
        assert "the person who sent you this page" not in chosen


def test_the_guide_quotes_labels_the_user_site_really_shows():
    guide = render("/docs/guide")
    states = {words for _, words, _ in usersite.STATE_WORDS.values()}
    for label in ("Setting up", "Ready to sign in", "In use"):
        assert label in states and f">{label}</span>" in guide
    source = inspect.getsource(usersite)
    for label in ("Claim a slot", "Sign in to Claude", "Send code", "Get a device token",
                  "Done with it", "Give this slot back", "Sign in again"):
        assert f'"{label}"' in source, f"the user site has no button {label!r}"
        assert f">{label}</span>" in guide


def test_the_terms_are_dated_and_say_slots_are_not_backed_up():
    terms = render("/docs/terms")
    assert f"Last updated {customer_docs.DOCS_UPDATED}" in terms
    assert "not backed up" in terms and "not backed up" in render("/docs/guide")


# -- the way in ----------------------------------------------------------------------

def test_the_bare_address_points_a_customer_home(one_site):
    """Somebody typing the bare address lands on the console's door; it tells
    them where their slots are and where to start."""
    status, body = one_site("/")
    assert status == 401
    # Said in the page itself, not left to the footer every page carries.
    card = body[body.index("Looking for your slots?"):]
    card = card[:card.index("</div>")]
    for target in ('href="/account"', 'href="/docs"', 'href="/docs/guide"'):
        assert target in card


def test_every_user_site_page_links_the_docs(one_site):
    status, body = one_site("/account")
    assert status == 200
    for target in ('href="/docs"', 'href="/docs/guide"', 'href="/docs/terms"',
                   'href="/privacy"'):
        assert target in body


def test_the_device_token_window_is_the_one_the_code_keeps(monkeypatch):
    assert "for at most 15 minutes, and never kept" in render("/docs/guide")
    monkeypatch.setattr(customer_docs, "LOGIN_MAX_AGE_S", 20 * 60)
    assert "for at most 20 minutes, and never kept" in render("/docs/guide")
