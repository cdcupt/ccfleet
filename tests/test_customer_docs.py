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
LANDING = "<h1>Claude Code on a machine that is always on</h1>"
HOW = ("How it works", "/docs/how-it-works")


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
    # The overview's own address is the bare one, so that is the link it marks.
    marked = "/" if path == "/docs" else path
    assert page.count(' class="here"') == 1
    assert f'<a href="{marked}" class="here">' in page
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


def test_the_landing_page_signs_in_with_google_only_where_that_works():
    """Straight into Google's sign-in when it is set up; otherwise the page that
    says it is not, rather than a button that goes nowhere."""
    ready = render("/docs", google_client_id="c", google_client_secret="s",
                   cookie_secret="0" * 32, public_url="https://fleet.example.com")
    assert 'href="/auth/google/start?next=/account">Sign in with Google' in ready
    unset = render("/docs")
    assert 'href="/account">Sign in with Google' in unset
    assert "/auth/google/start" not in unset


def test_the_landing_page_offers_somebody_signed_in_their_slots():
    """Not a sign-in they have already done."""
    viewer = usersite.Viewer({"id": "u1", "email": "erik@example.com", "role": "user"},
                             "t" * 64)
    shown = customer_docs.page_for("/docs")(Config(), viewer=viewer)
    hero = shown[shown.index('<div class="cta-row">'):]
    hero = hero[:hero.index("</div>")]
    assert 'href="/account">Your slots</a>' in hero
    assert "Sign in with Google" not in shown and "Already have a slot?" not in shown


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
                  "Done with it", "Give this slot back", "Sign in again", "Back to Stable"):
        assert f'"{label}"' in source, f"the user site has no button {label!r}"
        assert f">{label}</span>" in guide


def test_the_connect_commands_the_pages_quote_are_ones_the_script_has():
    """Like a button, a flag a page names has to exist: the script's own usage
    lines say so. And a computer uses one Claude account, so no page tells
    anybody to keep several on one and switch between them."""
    from pathlib import Path

    script = (Path(__file__).parents[1] / "laptop" / "ccfleet-connect.sh").read_text()
    usage = [line for line in script.splitlines() if line.startswith("#   ccfleet-connect")]
    pages = {"guide": render("/docs/guide"),
             "token page": usersite.token_page({"id": "s1"}, "sk-ant-oat01-" + "x" * 20)}
    for where, page in pages.items():
        for flag in re.findall(r"ccfleet-connect (--[a-z-]+)", page):
            assert any(f"ccfleet-connect {flag}" in line for line in usage), (where, flag)
        for gone in ("--add", "--use", "--list"):
            assert f"ccfleet-connect {gone}" not in page, (where, gone)
        assert "one Claude account" in page, where


def test_the_guide_names_the_line_a_slot_really_starts_with():
    """The guide tells people which line to edit to lower the effort. Naming a
    line or a file slot-add does not write would send them looking for nothing."""
    from pathlib import Path

    script = (Path(__file__).parents[1] / "node" / "slot-add.sh").read_text()
    guide = render("/docs/guide")
    assert "setdefault('model', 'opus')" in script and "<strong>Opus</strong>" in guide
    assert "setdefault('CLAUDE_CODE_EFFORT_LEVEL', 'max')" in script
    assert "<code>CLAUDE_CODE_EFFORT_LEVEL</code>" in guide and "max effort" in guide
    assert "expanduser('~/.claude/settings.json')" in script
    assert "<code>~/.claude/settings.json</code>" in guide


def test_the_terms_are_dated_and_say_slots_are_not_backed_up():
    terms = render("/docs/terms")
    assert f"Last updated {customer_docs.DOCS_UPDATED}" in terms
    assert "not backed up" in terms and "not backed up" in render("/docs/guide")


# -- the way in ----------------------------------------------------------------------

def test_the_bare_address_is_the_front_page(one_site):
    """The overview itself, never a redirect to it: the same page /docs is."""
    status, body = one_site("/")
    assert status == 200 and LANDING in body
    assert body == one_site("/docs")[1]
    assert one_site("/?signin=cancelled")[0] == 200


def test_with_two_sites_the_bare_address_is_the_products_front_page(two_sites):
    """The admin host's bare address is still the console's, since the
    console is all that host serves."""
    status, body = two_sites("/", host=PRODUCT)
    assert status == 200 and LANDING in body
    status, body = two_sites("/", host=ADMIN)
    assert status == 303 and LANDING not in body


def test_the_front_page_names_the_bare_address_as_its_own(one_site):
    """Served at / and at /docs alike, it says which address to keep, and no
    other page claims an address it is not at."""
    for path in ("/", "/docs", "/docs/"):
        status, body = one_site(path)
        head = body[:body.index("</head>")]
        assert status == 200 and head.count('rel="canonical"') == 1, path
        assert '<link rel="canonical" href="/">' in head, path
    for path in ("/docs/guide", "/docs/how-it-works", "/docs/terms", "/privacy", "/account"):
        assert 'rel="canonical"' not in one_site(path)[1], path


def test_the_wordmark_and_the_overview_link_go_to_the_bare_address(one_site):
    for path in ("/", *DOC_PATHS, "/privacy", "/account"):
        body = one_site(path)[1]
        assert body.count('<a class="brand" href="/">') == 2, path  # the bar and the footer
        nav = body[body.index('<nav class="doc-nav">'):]
        nav = nav[:nav.index("</nav>")]
        assert re.search(r'<a href="/"( class="here")?>Overview</a>', nav), path
        assert 'href="/docs"' not in nav, path


def _way_on(page):
    """The hero's buttons, as (label, href), in the order they are shown."""
    row = page[page.index('<div class="cta-row">'):]
    row = row[:row.index("</div>")]
    return [(label, href) for href, label
            in re.findall(r'<a class="btn[^"]*" href="([^"]*)">([^<]*)</a>', row)]


def _viewer(account, **links):
    return usersite.Viewer({"id": "u1", "email": "erik@example.com", **account}, "t" * 64,
                           **links)


@pytest.mark.parametrize("account,expected", [
    (None, [("Sign in with Google", "/account"), HOW]),
    ({"role": "user"}, [("Your slots", "/account"), HOW]),
    ({"role": "admin"}, [("Your slots", "/account"), ("Console", "/admin"), HOW]),
])
def test_the_front_pages_way_on_suits_whoever_is_looking(account, expected):
    viewer = None if account is None else _viewer(account)
    assert _way_on(customer_docs.overview(Config(), viewer=viewer)) == expected


@pytest.mark.parametrize("account", [{}, {"role": "user"}, {"role": "owner"},
                                     {"role": "Admin"}, {"role": "admin "}, {"role": ""}])
def test_only_an_operator_is_offered_the_console(account):
    """The button saves an operator a click, and the console checks the role
    again itself; but nobody else is ever shown a way in that is not theirs."""
    shown = customer_docs.overview(Config(), viewer=_viewer(account))
    seen = shown[shown.index("<body"):]  # the page, not the stylesheet's comments
    assert "Console" not in seen and "/admin" not in seen
    assert [label for label, _ in _way_on(shown)] == ["Your slots", "How it works"]


def test_the_front_pages_own_links_are_escaped():
    """The console's address comes from configuration and the canonical from
    the caller; neither is trusted to be free of markup."""
    viewer = _viewer({"role": "admin"}, slots_href='/account?a="><zz>',
                     console_href='https://adm.example.com/admin?b="><zz>&c')
    shown = customer_docs.overview(Config(), viewer=viewer)
    assert 'href="/account?a=&quot;&gt;&lt;zz&gt;">Your slots</a>' in shown
    assert 'href="https://adm.example.com/admin?b=&quot;&gt;&lt;zz&gt;&amp;c">Console</a>' in shown
    assert "<zz>" not in shown
    framed = usersite._shell("t", "", canonical='/"><zz>')
    assert '<link rel="canonical" href="/&quot;&gt;&lt;zz&gt;">' in framed
    assert "<zz>" not in framed
    assert 'rel="canonical"' not in usersite._shell("t", "")


def test_the_consoles_door_points_a_customer_home(one_site):
    """A customer who finds /admin anyway is told where their slots are and
    where to start."""
    status, body = one_site("/admin")
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
