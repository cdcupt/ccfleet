"""The public status page, and each holder's line about their slot's machine.

Public, so it says only what anybody may know: whether this site and the
machines are up, and how they have been. Never a machine's id or a slot's name,
since a held slot is named after its holder (Erik, 2026-09-24, #108).
"""
from __future__ import annotations

import re
import threading
import time

import pytest

from ccfleetd import render, status, statuspage, usersite
from ccfleetd.config import Config
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.rules import LEVEL_WARN
from tests.conftest import refresh_of
from tests.test_admin_host import ADMIN, PRODUCT, split  # noqa: F401  (the fixture)
from tests.test_usersite import report, site  # noqa: F401  (the fixture, by name)
from tests.test_usersite_v2 import one_slot_machine, whole_card

NOW = 1_790_000_040.0
NAMES = ("pool-7", "pool-8", "erik-9", "ana-1", "ana@example.com", "erik@example.com",
         "slot01", "us-west")


def fleet(store, now=NOW, ages=(20, 30, 40)):
    """As live: somebody's own node, and two shared machines of which one is
    held, so its slot is named after its holder."""
    store.add_account("a1", "sub-a1", "ana@example.com", slot_quota=1, now=now - 86400)
    store.add_account("e1", "sub-e1", "erik@example.com", slot_quota=3, now=now - 86400)
    store.set_account_handle("a1", "ana")          # a name that is plainly a person's
    store.add_node("erik-9", "erik", region="us-west", now=now - 86400)
    store.hold_owner_node("erik-9", "e1", unix_user="erik", now=now - 86400)
    for node in ("pool-7", "pool-8"):
        store.add_node(node, "op", region="us-west", now=now - 86400)
        store.add_slot(node, node, "slot01", now=now - 86400)
        store.apply_slot_report(node, [{"unix_user": "slot01", "present": False}],
                                now=now - 86400)
    held = store.claim_slot("a1", now=now - 3600, node_id="pool-7")
    assert held["name"] == "ana-1"
    store.insert_heartbeat("erik-9", now - ages[0], {"node_id": "erik-9"})
    for node, age in (("pool-7", ages[1]), ("pool-8", ages[2])):
        store.insert_heartbeat(node, now - age, {"node_id": node, "mode": "machine",
                                                 "slots": []})


def page_of(store, cfg, now=NOW):
    return statuspage.page(store, cfg, None, now)


def shown(page):
    """What a person reads: the tags gone."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))


# -- now ------------------------------------------------------------------------------------

def test_everything_up_is_all_systems_operational(store, cfg):
    fleet(store)
    page = shown(page_of(store, cfg))
    assert "All systems operational" in page and "3 of 3 operational" in page
    assert "Website and account pages" in page and "Slot machines" in page


def test_one_machine_down_is_a_partial_outage_and_says_since_when(store, cfg):
    fleet(store, ages=(20, 30, 400))
    raw = page_of(store, cfg)
    assert "Partial outage" in shown(raw) and "2 of 3 operational" in shown(raw)
    assert "1 down" in shown(raw)
    since = status.utc_iso(NOW - 400 + 300)
    assert f'<time datetime="{since}" data-local>' in raw


def test_a_degraded_machine_is_a_partial_outage_too(store, cfg):
    fleet(store)
    store.open_alert("pool-8", "disk_high", LEVEL_WARN, "disk 88% used", NOW - 120)
    page = shown(page_of(store, cfg))
    assert "Partial outage" in page and "1 degraded" in page


def test_every_machine_down_is_a_major_outage(store, cfg):
    fleet(store, ages=(2000, 2000, 2000))
    page = shown(page_of(store, cfg))
    assert "Major outage" in page and "0 of 3 operational" in page


def test_no_machines_yet(store, cfg):
    raw = page_of(store, cfg)
    page = shown(raw)
    assert "All systems operational" in page and "No slot machines yet" in page
    assert "No uptime counted yet" in page
    assert 'aria-label="Website and account pages: no data yet"' in raw


# -- what it never says ----------------------------------------------------------------------

def test_the_page_names_no_machine_and_nobody(store, cfg):
    fleet(store, ages=(20, 30, 400))
    store.open_alert("pool-7", "disk_high", LEVEL_WARN, "disk 88% used on pool-7", NOW - 60)
    for minute in range(3):
        status.record(store, NOW + 60 * minute, check_interval_s=60)
    raw = page_of(store, cfg, NOW + 180)
    for name in NAMES:
        assert name not in raw, name


def test_the_page_carries_no_script_but_the_local_times_one(store, cfg):
    fleet(store)
    raw = page_of(store, cfg)
    assert raw.count("<script") == 1 and render.LOCAL_TIMES_TAG in raw


def test_the_page_refreshes_itself_every_minute(store, cfg):
    assert refresh_of(page_of(store, cfg)) == (60, "")


# -- the ninety days ---------------------------------------------------------------------------

def bars(raw):
    return re.findall(r'<div class="st-bar"[^>]*>(.*?)</div>', raw, re.S)


def test_each_component_has_a_bar_of_ninety_days(store, cfg):
    fleet(store)
    found = bars(page_of(store, cfg))
    assert len(found) == 2
    for bar in found:
        assert len(re.findall(r"<span class=\"d ", bar)) == status.HISTORY_DAYS


def test_a_day_says_what_it_was_on_hover_and_to_a_screen_reader(store, cfg):
    fleet(store)
    status.record(store, NOW, check_interval_s=60)
    raw = page_of(store, cfg, NOW + 60)
    today = status.utc_day(int(NOW // 60))
    assert f'title="{today} (UTC): 100% up"' in raw
    assert re.search(r'role="img" aria-label="Website and account pages: 100% uptime', raw)


def test_uptime_is_shown_for_each_component_and_overall(store, cfg):
    fleet(store)
    store.count_status(status.SITE, status.GREEN, NOW, grace=2, gap_state=status.RED)
    store.count_status(status.SITE, status.GREEN, NOW + 3 * 60, grace=0, gap_state=status.RED)
    store.count_status("node:pool-7", status.GREEN, NOW, grace=2)
    store.count_status("node:pool-8", status.YELLOW, NOW, grace=2)     # degraded is up
    store.count_status(status.GROUP, status.YELLOW, NOW, grace=2)
    raw = page_of(store, cfg, NOW + 240)
    page = shown(raw)
    assert "50.00% uptime" in page                 # the site: 2 up, 2 down
    assert "100% uptime" in page                   # the machines: 2 machine-minutes, both up
    assert "66.66% uptime over the last 90 days" in page    # 4 up of 6 counted
    assert ('aria-label="Website and account pages: 50.00% uptime over the last 90 days; '
            '1 day with trouble"') in raw
    assert 'aria-label="Slot machines: 100% uptime over the last 90 days; 1 day with trouble"' in raw


def test_each_day_says_what_it_was(store, cfg):
    today = status.utc_day(int(NOW // 60))
    yesterday = status.utc_day(int(NOW // 60) - 1440)
    store.count_status(status.SITE, status.GREEN, NOW, grace=2, gap_state=status.RED)
    store.count_status(status.SITE, status.GREEN, NOW + 3 * 60, grace=0, gap_state=status.RED)
    for i, state in enumerate([status.RED, status.RED, status.YELLOW]):
        store.count_status(status.GROUP, state, NOW + 60 * i, grace=2)
    raw = page_of(store, cfg, NOW + 240)
    assert f'title="{today} (UTC): 50.00% up, down 2 min"' in raw
    assert f'title="{today} (UTC): every machine down 2 min, some in trouble 1 min"' in raw
    assert f'title="{yesterday} (UTC): no data"' in raw
    store.count_status(status.GROUP, status.GREEN, NOW + 86400, grace=2)
    tomorrow = status.utc_day(int(NOW // 60) + 1440)
    assert f'title="{tomorrow} (UTC): all up"' in page_of(store, cfg, NOW + 86400 + 60)


# -- where it is ----------------------------------------------------------------------------

def test_the_status_page_is_public_on_the_product_and_absent_from_the_console(split):  # noqa: F811
    _, browser, _ = split
    reply = browser(PRODUCT).call("GET", "/status")
    assert reply.status == 200 and "All systems operational" in reply.body
    assert browser(ADMIN).call("GET", "/status").status == 404


def test_every_public_page_links_it_from_the_footer_and_the_front_page_from_its_body(split):  # noqa: F811
    _, browser, _ = split
    for path in ("/", "/docs/guide", "/privacy"):
        foot = browser(PRODUCT).call("GET", path).body
        foot = foot[foot.index('<footer class="sitefoot">'):]
        assert 'href="/status"' in foot, path
    front = browser(PRODUCT).call("GET", "/").body
    assert 'href="/status"' in front[:front.index('<footer class="sitefoot">')]


def test_signed_in_the_only_address_on_it_is_the_viewers_own_in_their_menu(site):  # noqa: F811
    """Codex, PR #111: signed in, the page wears the same bar as every public
    page, whose menu shows the viewer their own address. Nobody else's name,
    address or machine is anywhere on it."""
    store, sign_in, _ = site
    fleet(store, now=time.time())
    viewer = sign_in(sub="google-viewer", email="viewer@example.com")
    raw = viewer.call("GET", "/status").body
    menu_at = raw.index('<details class="usermenu">')
    menu = raw[menu_at:raw.index("</details>", menu_at) + len("</details>")]
    assert "viewer@example.com" in menu
    rest = raw.replace(menu, "")
    assert not re.search(r"[\w.+-]+@[\w-]+\.\w+", rest)
    for name in NAMES:
        assert name not in rest, name


def test_how_it_works_says_where_to_look(split):  # noqa: F811
    _, browser, _ = split
    page = browser(PRODUCT).call("GET", "/docs/how-it-works").body
    assert 'href="/status"' in page[:page.index('<footer class="sitefoot">')]


# -- the holder's line ----------------------------------------------------------------------

def test_the_line_says_the_state_and_when_it_began():
    green = status.slot_line(status.State(status.GREEN), NOW)
    assert "Machine: operational" in shown(green) and 'href="/status"' in green
    late = status.slot_line(status.State(status.YELLOW, NOW - 300), NOW)
    assert "Machine: degraded since" in shown(late)
    assert f'<time datetime="{status.utc_iso(NOW - 300)}" data-local>5m ago</time>' in late
    down = status.slot_line(status.State(status.RED), NOW)
    assert "Machine: down" in shown(down) and "since" not in down


def claimed_card(site_, age=5, alert=None):
    store, sign_in, _ = site_
    one_slot_machine(store, "pool-1")
    erik = sign_in(quota=1)
    assert erik.press("/account/claim").status == 303
    report(store, "pool-1", [{"unix_user": "slot01", "present": True}], ts=time.time() - age)
    if alert:
        store.open_alert("pool-1", alert[0], alert[1], "x", time.time() - 30)
    return shown(whole_card(erik.page(), "pool-1"))


def test_a_slot_card_says_its_machine_is_up(site):  # noqa: F811
    assert "Machine: operational" in claimed_card(site)


def test_the_dots_are_styled_wherever_the_line_or_the_page_is(store, cfg):
    """The state's colour is a styled dot: both pages carry its rules."""
    assert ".st-dot.green{" in page_of(store, cfg)
    assert ".st-dot.green{" in usersite._shell("x", "")


def test_a_slot_card_says_its_machine_is_down(site):  # noqa: F811
    assert "Machine: down since" in claimed_card(site, age=400)


def test_a_slot_card_says_its_machine_is_degraded(site):  # noqa: F811
    assert "Machine: degraded since" in claimed_card(site, alert=("disk_high", LEVEL_WARN))


def test_somebodys_own_node_is_judged_by_its_own_five_minute_beat(site):  # noqa: F811
    """An owner's node reports every five minutes: six minutes quiet is normal
    for it, where a shared machine would be down."""
    store, sign_in, _ = site
    erik = sign_in(quota=1)
    store.add_node("erik-1", "erik", now=time.time())
    store.hold_owner_node("erik-1", erik.account["id"], unix_user="erik", now=time.time())
    store.insert_heartbeat("erik-1", time.time() - 400, {"node_id": "erik-1"})
    assert "Machine: operational" in shown(whole_card(erik.page(), "erik-1"))


# -- counted once a minute by the server that serves it ------------------------------------------

class OneRound:
    """A stop event that lets run_forever go round once."""

    def __init__(self):
        self.rounds = 0

    def is_set(self):
        self.rounds += 1
        return self.rounds > 1

    def wait(self, _seconds):
        return None


def test_the_serving_loop_counts_the_minute(store, cfg):
    fleet(store, now=time.time())
    Monitor(store, cfg, LogNotifier()).run_forever(OneRound())
    components = {r["component"] for r in store.status_minutes(since_day="2000-01-01")}
    assert components == {status.SITE, status.GROUP, "node:pool-7", "node:pool-8",
                          "node:erik-9"}


def test_a_check_that_fails_still_counts_the_minute(store, cfg, monkeypatch):
    """The server is up whether or not one check went wrong."""
    monitor = Monitor(store, cfg, LogNotifier())
    monkeypatch.setattr(monitor, "check_all", lambda *a, **k: 1 / 0)
    monitor.run_forever(OneRound())
    assert [r["component"] for r in store.status_minutes(since_day="2000-01-01")] == [
        status.SITE]


def test_a_check_run_by_hand_counts_nothing(store, cfg):
    """`ccfleetd check` runs check_all beside a server that may be down: only
    the serving loop's minute says the site is up."""
    fleet(store, now=time.time())
    Monitor(store, cfg, LogNotifier()).check_all()
    assert store.status_minutes(since_day="2000-01-01") == []


def test_the_grace_follows_the_configured_check_interval(store):
    cfg = Config.from_env({"CCFLEET_ADMIN_TOKEN": "x" * 32, "CCFLEET_DB": ":memory:",
                           "CCFLEET_CHECK_INTERVAL_S": "300"})
    monitor = Monitor(store, cfg, LogNotifier(), clock=lambda: NOW)
    monitor.record_status(NOW)
    monitor.record_status(NOW + 300)                      # four minutes unseen: normal here
    [row] = store.status_minutes(since_day="2000-01-01")
    assert (row["green"], row["red"]) == (6, 0)


def test_status_is_recorded_under_the_monitor_lock(store, cfg):
    """One writer at a time with the checks, which read the same heartbeats."""
    monitor = Monitor(store, cfg, LogNotifier())
    monitor._lock.acquire()
    worker = threading.Thread(target=monitor.record_status, args=(NOW,))
    worker.start()
    worker.join(timeout=0.3)
    assert worker.is_alive(), "recorded without the lock"
    monitor._lock.release()
    worker.join(timeout=5)
    assert not worker.is_alive()


# -- the front page's pill (Erik, 2026-09-25) ---------------------------------------------------

def pill_on(page):
    """The front page's status pill: the link that says the banner's words."""
    found = re.search(r'<a class="pill st-pill[^"]*" href="/status"[^>]*>[^<]*</a>', page)
    assert found, "no status pill"
    return found.group(0)


@pytest.mark.parametrize("ages, alert, level, cls", [
    ((20, 30, 40), None, status.GREEN, "ok"),
    ((20, 30, 400), None, status.YELLOW, "warn"),
    ((20, 30, 40), ("disk_high", LEVEL_WARN), status.YELLOW, "warn"),
    ((2000, 2000, 2000), None, status.RED, "critical"),
])
def test_the_pill_says_what_the_banner_says_in_its_colour(store, cfg, ages, alert, level, cls):
    fleet(store, ages=ages)
    if alert:
        store.open_alert("pool-8", alert[0], alert[1], "x", NOW - 120)
    banner = statuspage.BANNERS[level]
    assert f"<strong>{banner}</strong>" in page_of(store, cfg)
    assert statuspage.health(store, NOW) == level
    pill = statuspage.pill(level)
    assert shown(pill).strip() == banner and f'class="pill st-pill {cls}"' in pill
    assert f'aria-label="Status: {banner}"' in pill


def test_with_no_machine_counted_the_pill_is_a_plain_link_never_a_green(store):
    """The status page says all is well with nothing to count. The front page
    saying it too would vouch for machines nobody has measured."""
    assert statuspage.health(store, NOW) is None
    pill = statuspage.pill(None)
    assert shown(pill).strip() == "Status" and pill.startswith('<a class="pill st-pill"')
    assert "ok" not in pill and "operational" not in pill


def test_the_front_page_says_it_at_its_top_and_no_other_page_does(split):  # noqa: F811
    store, browser, _ = split
    fleet(store, now=time.time())
    for path in ("/", "/docs"):
        page = browser(PRODUCT).call("GET", path).body
        main = page[page.index("<main"):]
        assert main.index('class="pill st-pill') < main.index("<h1>"), path
        assert shown(pill_on(page)).strip() == "All systems operational", path
        assert page.count("<script") == 1, "no script of its own"
        assert ".st-pill{" in page and ".pill.ok{" in page, "styled, from the theme's tokens"
    assert 'class="pill st-pill' not in browser(PRODUCT).call("GET", "/docs/guide").body


def test_the_front_page_says_trouble_and_names_nobody(split):  # noqa: F811
    store, browser, _ = split
    fleet(store, now=time.time(), ages=(20, 30, 400))
    page = browser(PRODUCT).call("GET", "/").body
    assert shown(pill_on(page)).strip() == "Partial outage"
    for name in NAMES:
        assert name not in page, name


def test_a_front_page_with_no_machine_yet_says_only_status(split):  # noqa: F811
    _, browser, _ = split
    assert shown(pill_on(browser(PRODUCT).call("GET", "/").body)).strip() == "Status"
