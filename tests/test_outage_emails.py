"""Outage emails (Erik, 2026-09-24): to holders who asked, when their slot's
machine has been down five minutes and when it is back; the site's own outage
said once it is over. Sent through Resend from the operator's own address."""
from __future__ import annotations

import json
import logging
import urllib.error

import pytest

from ccfleetd import mail, outage, slots
from ccfleetd.config import Config
from ccfleetd.mail import Email
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.status import GREEN, RED, YELLOW, State

from .test_usersite import claimed, machine, report, serving  # noqa: F401

NOW = 1_790_000_000.0
URL = "https://fleet.example"
KEY = "re_key_secret_value"


# -- sending ------------------------------------------------------------------------------

class Reply:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener(status=200, raises=None):
    seen = []

    def open_(request, timeout):
        seen.append((request, timeout))
        if raises is not None:
            raise raises
        return Reply(status)
    return open_, seen


def an_email(to="ana@example.com"):
    return Email(to=to, subject="Hi", text="text", html="<p>text</p>")


def test_a_send_is_one_request_to_resend_that_names_itself():
    open_, seen = opener()
    assert mail.ResendMailer(KEY, "ccfleet <ccfleet@example.com>", open_).send(an_email())
    [(request, timeout)] = seen
    assert request.full_url == mail.RESEND_URL and request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {KEY}"
    assert request.get_header("User-agent") == mail.USER_AGENT
    assert "Python-urllib" not in mail.USER_AGENT, "Cloudflare refuses urllib's own"
    assert json.loads(request.data) == {"from": "ccfleet <ccfleet@example.com>",
                                        "to": ["ana@example.com"], "subject": "Hi",
                                        "text": "text", "html": "<p>text</p>"}
    assert timeout == mail.SEND_TIMEOUT_S


@pytest.mark.parametrize("status", [199, 300, 500])
def test_a_reply_that_is_not_success_is_not_sent(status):
    open_, _ = opener(status)
    assert not mail.ResendMailer(KEY, "x <x@example.com>", open_).send(an_email())


@pytest.mark.parametrize("exc", [
    urllib.error.HTTPError(mail.RESEND_URL, 403, "no", {}, None),
    urllib.error.URLError("down"), TimeoutError(), OSError("reset"), ValueError("bad")])
def test_a_failed_send_says_so_without_the_address_or_key(exc, caplog):
    open_, _ = opener(raises=exc)
    with caplog.at_level(logging.WARNING, logger="ccfleetd.mail"):
        assert not mail.ResendMailer(KEY, "x <x@example.com>", open_).send(
            an_email("ana.person@example.com"))
    assert "a…@example.com" in caplog.text
    assert "ana.person" not in caplog.text and KEY not in caplog.text


def test_emails_are_sent_only_when_the_operator_set_both_key_and_sender():
    assert isinstance(mail.build_mailer(Config()), mail.NoMailer)
    assert isinstance(mail.build_mailer(Config(resend_api_key=KEY)), mail.NoMailer)
    assert isinstance(mail.build_mailer(Config(email_from="x <x@example.com>")), mail.NoMailer)
    assert isinstance(mail.build_mailer(Config(resend_api_key=KEY,
                                               email_from="x <x@example.com>")),
                      mail.ResendMailer)
    assert not mail.NoMailer().send(an_email())


def test_the_settings_come_from_the_environment():
    cfg = Config.from_env({"CCFLEET_RESEND_API_KEY": f" {KEY} ",
                           "CCFLEET_EMAIL_FROM": " ccfleet <ccfleet@example.com> "})
    assert cfg.resend_api_key == KEY and cfg.email_from == "ccfleet <ccfleet@example.com>"
    assert cfg.emails_ready and not Config.from_env({}).emails_ready
    assert not Config(resend_api_key=KEY).emails_ready
    assert not Config(email_from="x <x@example.com>").emails_ready


def test_an_address_is_masked_to_its_first_letter_and_domain():
    assert mail.masked("ana@example.com") == "a…@example.com"
    assert mail.masked("nobody") == "…"


# -- when to tell ------------------------------------------------------------------------

def fleet(store, *, opted=("a1",)):
    """Machine m1 held by a1 and b1's slot on m2; a3 holds nothing."""
    for node in ("m1", "m2"):
        store.add_node(node, "op", now=NOW)
        store.add_slot(node, node, "slot01", now=NOW)
        store.apply_slot_report(node, [{"unix_user": "slot01", "present": False}], now=NOW)
    for account, node in (("a1", "m1"), ("b1", "m2")):
        store.add_account(account, f"sub-{account}", f"{account}@example.com", slot_quota=1,
                          now=NOW)
        store.claim_slot(account, now=NOW, node_id=node)
    store.add_account("a3", "sub-a3", "a3@example.com", slot_quota=1, now=NOW)
    for account in opted:
        store.set_outage_emails(account, True)


def test_a_machine_down_under_five_minutes_is_not_news(store):
    fleet(store)
    assert outage.machine_outages(store, {"m1": State(RED, NOW)}, NOW + 299, URL) == []
    assert outage.machine_outages(store, {"m1": State(GREEN)}, NOW + 330, URL) == []
    assert store.open_outage("m1") is None


def test_five_minutes_down_tells_the_holders_who_asked_and_only_once(store):
    fleet(store, opted=("a1", "b1"))
    sent = outage.machine_outages(store, {"m1": State(RED, NOW), "m2": State(GREEN)},
                                  NOW + 300, URL)
    assert [e.to for e in sent] == ["a1@example.com"], "only m1's holder, not m2's"
    assert "is down" in sent[0].subject and "since 2026-09-21 14:13 UTC" in sent[0].text
    assert f"{URL}/status" in sent[0].text and f"{URL}/account" in sent[0].text
    assert outage.machine_outages(store, {"m1": State(RED, NOW)}, NOW + 600, URL) == []


def test_somebody_who_did_not_ask_is_not_told(store):
    fleet(store, opted=())
    assert outage.machine_outages(store, {"m1": State(RED, NOW)}, NOW + 400, URL) == []


def test_back_is_told_once_to_those_told_it_was_down(store):
    fleet(store)
    outage.machine_outages(store, {"m1": State(RED, NOW)}, NOW + 300, URL)
    sent = outage.machine_outages(store, {"m1": State(YELLOW, NOW + 900)}, NOW + 900, URL)
    assert [e.to for e in sent] == ["a1@example.com"] and "is back" in sent[0].subject
    assert "after 15 min down" in sent[0].text
    assert store.open_outage("m1") is None
    assert outage.machine_outages(store, {"m1": State(GREEN)}, NOW + 960, URL) == []


def test_a_new_outage_is_told_again(store):
    fleet(store)
    outage.machine_outages(store, {"m1": State(RED, NOW)}, NOW + 300, URL)
    outage.machine_outages(store, {"m1": State(GREEN)}, NOW + 400, URL)
    sent = outage.machine_outages(store, {"m1": State(RED, NOW + 1000)}, NOW + 1300, URL)
    assert [e.to for e in sent] == ["a1@example.com"]


def test_a_machine_that_stops_counting_ends_its_outage_quietly(store):
    fleet(store)
    outage.machine_outages(store, {"m1": State(RED, NOW)}, NOW + 300, URL)
    assert outage.machine_outages(store, {}, NOW + 400, URL) == []
    assert store.open_outage("m1") is None


def test_a_red_with_no_known_start_starts_now(store):
    fleet(store)
    assert outage.machine_outages(store, {"m1": State(RED)}, NOW, URL) == []
    assert store.open_outage("m1")["started_at"] == NOW
    assert len(outage.machine_outages(store, {"m1": State(RED)}, NOW + 300, URL)) == 1


def test_a_slot_given_back_is_not_told(store):
    fleet(store)
    store.begin_release("m1")
    assert outage.machine_outages(store, {"m1": State(RED, NOW)}, NOW + 300, URL) == []


def test_the_email_says_the_slots_own_name_and_a_long_outage_in_hours(store):
    fleet(store)
    outage.machine_outages(store, {"m1": State(RED, NOW)}, NOW + 300, URL)
    [back] = outage.machine_outages(store, {"m1": State(GREEN)}, NOW + 2 * 3600 + 300, URL)
    name = store.get_slot("m1")["name"]
    assert name in back.subject and "after 2h 5m down" in back.text
    assert back.html.startswith("<p>") and "<script" not in back.html


# -- the site ------------------------------------------------------------------------------

def test_a_site_that_was_silent_five_minutes_says_so_once_back(store):
    fleet(store, opted=("a1", "b1"))
    last = int(NOW // 60) - 1                       # counted up to the minute before
    assert outage.site_back(store, last, NOW + 5 * 60, 2, URL) == []   # within grace
    sent = outage.site_back(store, last, NOW + 7 * 60 + 1, 2, URL)
    assert sorted(e.to for e in sent) == ["a1@example.com", "b1@example.com"]
    assert "kept working" in sent[0].text and "was down" in sent[0].subject


def test_a_site_never_counted_says_nothing(store):
    fleet(store)
    assert outage.site_back(store, None, NOW, 2, URL) == []


def test_the_lasting_words():
    assert outage.lasted(30) == "1 min" and outage.lasted(15 * 60) == "15 min"
    assert outage.lasted(3 * 3600 + 7 * 60) == "3h 7m"


# -- the monitor ---------------------------------------------------------------------------

class Outbox:
    def __init__(self):
        self.sent = []

    def send(self, email):
        self.sent.append(email)
        return True


def test_the_minute_loop_sends_what_is_owed_once(store):
    fleet(store)
    store.insert_heartbeat("m1", NOW, {"node_id": "m1", "mode": "machine", "slots": []})
    store.insert_heartbeat("m2", NOW + 590, {"node_id": "m2", "mode": "machine", "slots": []})
    outbox = Outbox()
    monitor = Monitor(store, Config(public_url=URL), LogNotifier(), mailer=outbox)
    monitor.record_status(NOW + 300)            # m1 down since NOW + 300: not yet news
    monitor.record_status(NOW + 600)
    assert [e.to for e in outbox.sent] == ["a1@example.com"]
    monitor.record_status(NOW + 660)
    assert len(outbox.sent) == 1


def test_without_a_mailer_nothing_is_sent_and_nothing_breaks(store):
    fleet(store)
    store.insert_heartbeat("m1", NOW, {"node_id": "m1", "mode": "machine", "slots": []})
    Monitor(store, Config(public_url=URL), LogNotifier()).record_status(NOW + 900)
    assert store.open_outage("m1")["down_sent_at"] is not None


# -- the page --------------------------------------------------------------------------------

@pytest.fixture
def mail_site(monkeypatch):
    yield from serving(monkeypatch, resend_api_key=KEY, email_from="x <x@example.com>")


@pytest.fixture
def plain_site(monkeypatch):
    yield from serving(monkeypatch)


def test_outage_emails_are_off_until_turned_on_and_can_be_turned_off(mail_site):
    store, sign_in, _ = mail_site
    erik = sign_in(quota=1)
    page = erik.page()
    assert "<h2>Outage emails</h2>" in page and "Turn on" in page and "Resend" in page
    assert erik.press("/account/outage-emails", on="1").status == 303
    assert store.get_account(erik.account["id"])["outage_emails"] == 1
    assert "Turn off" in erik.page()
    reply = erik.press("/account/outage-emails", on="0")
    assert "note=emails-off" in reply.getheader("Location")
    assert store.get_account(erik.account["id"])["outage_emails"] == 0


def test_no_emails_configured_offers_none(plain_site):
    store, sign_in, _ = plain_site
    erik = sign_in(quota=1)
    assert "Outage emails" not in erik.page()
    assert erik.press("/account/outage-emails", on="1").status == 404
    assert store.get_account(erik.account["id"])["outage_emails"] == 0


def test_the_privacy_page_names_resend_and_what_goes_to_it(plain_site):
    store, sign_in, _ = plain_site
    page = sign_in().call("GET", "/privacy").body
    assert "go to Resend" in page and "outage emails" in page


def test_turning_emails_on_for_nobody_is_refused(store):
    from ccfleetd.store import StoreError
    with pytest.raises(StoreError):
        store.set_outage_emails("ghost", True)


def test_given_back_slots_and_others_do_not_count_as_recipients(store):
    fleet(store, opted=("a1", "b1", "a3"))
    assert [r["email"] for r in store.outage_emails_for("m1")] == ["a1@example.com"]
    assert store.outage_subscribers() == ["a1@example.com", "a3@example.com",
                                          "b1@example.com"]
    assert slots.RELEASING not in (s["state"] for s in store.list_slots(node_id="m1"))


def test_an_outage_follows_its_machine_to_a_new_name(store):
    """Renamed mid-outage, the machine is the same one: its holders are told it
    is back once, not that a new outage began."""
    fleet(store)
    outage.machine_outages(store, {"m1": State(RED, NOW)}, NOW + 300, URL)
    store.rename_node("m1", "m9")
    assert store.open_outage("m9")["down_sent_at"] is not None
    sent = outage.machine_outages(store, {"m9": State(GREEN)}, NOW + 900, URL)
    assert [e.to for e in sent] == ["a1@example.com"] and "is back" in sent[0].subject


def test_a_rename_drops_what_a_removed_machine_left_under_the_new_name(store):
    fleet(store)
    store.begin_outage("m7", NOW)                  # left by a machine long gone
    store.rename_node("m1", "m7")
    assert store.open_outage("m7") is None


def test_only_an_outages_own_moments_can_be_set(store):
    outage_id = store.begin_outage("m1", NOW)["id"]
    for column in ("started_at", "component", "down_sent_at = 0, started_at"):
        with pytest.raises(ValueError):
            store.mark_outage(outage_id, column, NOW)


def test_the_minute_loop_tells_of_the_sites_own_silence_once_back(store):
    fleet(store)
    monitor = Monitor(store, Config(public_url=URL), LogNotifier(), mailer=Outbox())
    monitor.record_status(NOW)                          # the site counted at NOW
    monitor.record_status(NOW + 60)
    assert monitor._mailer.sent == []
    monitor.record_status(NOW + 60 + 10 * 60)           # silent for ten minutes
    assert [e.to for e in monitor._mailer.sent] == ["a1@example.com"]
    assert "website was down" in monitor._mailer.sent[0].subject
