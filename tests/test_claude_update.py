"""Claude Code updates from the page (Erik, 2026-09-24).

A holder sees the version their slot runs and the latest one Anthropic has
published, and one press moves the slot there: the slot switches to the latest
channel, so the daily stable check cannot undo it, and an update is asked for,
so the machine installs now instead of at its next check. The operator's exact
pin on a shared machine is a hold nobody on a page can move.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
import time

import pytest

from ccfleetd import claude_versions, slots, usersite
from ccfleetd.claude_versions import Target
from ccfleetd.desired import IDLE_POLL_S, LOGIN_POLL_S, desired_state
from ccfleetd.heartbeat import validate_heartbeat
from ccfleetd.monitor import LOGIN_MAX_AGE_S, Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import NotYours, Store, StoreError
from tests.conftest import heartbeat, refresh_of
from tests.test_api import _machine_payload, call, server  # noqa: F401  (fixture)
from tests.test_console_slots import console, shared  # noqa: F401  (fixture)
from tests.test_usersite import claimed, machine, report, site  # noqa: F401  (fixture)

LATEST, STABLE = "2.1.281", "2.1.273"
BODIES = {"https://downloads.claude.ai/claude-code-releases/latest": LATEST + "\n",
          "https://downloads.claude.ai/claude-code-releases/stable": STABLE + "\n"}


def channels_at(now, latest=LATEST, stable=STABLE):
    return {"checked_at": now, "latest": {"version": latest, "fetched_at": now},
            "stable": {"version": stable, "fetched_at": now}}


def fetcher(bodies=None, calls=None):
    def fetch(url):
        if calls is not None:
            calls.append(url)
        answer = (BODIES if bodies is None else bodies)[url]
        if isinstance(answer, Exception):
            raise answer
        return answer
    return fetch


def text(fragment):
    """What a person reads: the tags gone, the entities said."""
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


def cc_row(page):
    start = page.find('<div class="row-line cc">')
    if start < 0:
        return ""
    end = page.find('<div class="row-line', start + 1)
    return page[start:end if end > 0 else len(page)]


# -- the release channels ----------------------------------------------------------

def test_versions_compare_as_numbers_and_an_unknown_one_is_never_older():
    assert claude_versions.is_newer("2.1.281", "2.1.273")
    assert claude_versions.is_newer("2.1.1000", "2.1.999"), "numbers, not letters"
    assert not claude_versions.is_newer("2.1.273", "2.1.273")
    for unknown in (None, "", "latest", "2.1", "2.1.281-beta", "<b>9.9.9</b>"):
        assert not claude_versions.is_newer(unknown, "2.1.1")
        assert not claude_versions.is_newer("9.9.9", unknown)


@pytest.mark.parametrize("body", ["<html>not found</html>", "2.1", "2.1.281-beta",
                                  "1.2.3.4", "v2.1.281", "2.1.281 2.1.282", "",
                                  "2.1.281" + " " * 70])
def test_a_channel_that_does_not_answer_with_a_version_is_not_believed(body):
    fetch = fetcher({claude_versions.RELEASES_URL.format(channel="latest"): body})
    assert claude_versions.fetch_channel("latest", fetch) is None


def test_a_channel_read_takes_the_bare_version_the_installer_reads():
    assert claude_versions.fetch_channel("latest", fetcher()) == LATEST
    assert claude_versions.fetch_channel("stable", fetcher()) == STABLE


def test_the_channels_are_read_about_once_an_hour():
    calls = []
    first = claude_versions.refresh({}, 1000.0, fetcher(calls=calls))
    assert first == channels_at(1000.0)
    assert claude_versions.refresh(first, 1000.0 + 3599, fetcher(calls=calls)) is None
    assert len(calls) == 2, "nothing read again inside the hour"
    assert claude_versions.refresh(first, 1000.0 + 3600, fetcher(calls=calls)) is not None


@pytest.mark.parametrize("failure", [OSError("no route"), TimeoutError("slow"), "<html>"])
def test_a_failed_read_keeps_the_last_good_number_and_when_it_was_read(failure):
    known = channels_at(1000.0)
    bodies = dict(BODIES)
    bodies[claude_versions.RELEASES_URL.format(channel="latest")] = failure
    later = claude_versions.refresh(known, 9000.0, fetcher(bodies))
    assert later["latest"] == {"version": LATEST, "fetched_at": 1000.0}, "kept, with its age"
    assert later["stable"] == {"version": STABLE, "fetched_at": 9000.0}
    assert later["checked_at"] == 9000.0, "and tried again in an hour, not on every pass"


def test_a_stored_record_is_checked_again_on_the_way_out():
    raw = json.dumps({"checked_at": 5, "latest": {"version": "<script>", "fetched_at": 5},
                      "stable": {"version": STABLE, "fetched_at": "x"}})
    assert claude_versions.from_json(raw) == {
        "checked_at": 5.0, "stable": {"version": STABLE, "fetched_at": None}}
    assert claude_versions.from_json("not json") == {}
    assert claude_versions.from_json(json.dumps([1, 2])) == {}


# -- who decides a slot's version --------------------------------------------------

@pytest.mark.parametrize("chosen", [None, "stable", "latest"])
def test_the_operators_exact_pin_is_a_hold_whatever_the_holder_chose(chosen):
    target = claude_versions.slot_target({"kind": slots.MACHINE_SLOT, "claude_channel": chosen},
                                         {"pinned_version": "2.1.278"})
    assert target == Target("2.1.278", "", True)


def test_the_holders_channel_beats_the_machines():
    machine_slot = {"kind": slots.MACHINE_SLOT, "claude_channel": "latest"}
    assert claude_versions.slot_target(machine_slot, {"pinned_version": "stable"}) == \
        Target("latest", "latest", False)
    assert claude_versions.slot_target({**machine_slot, "claude_channel": None},
                                       {"pinned_version": "stable"}) == \
        Target("stable", "stable", False)
    assert claude_versions.slot_target({**machine_slot, "claude_channel": None}, {}) == \
        Target("", "", False)


def test_an_owners_own_node_is_never_held_even_on_an_exact_pin():
    own = {"kind": slots.OWNER_SLOT}
    assert claude_versions.slot_target(own, {"pinned_version": "2.1.200"}) == \
        Target("2.1.200", "", False)
    assert claude_versions.slot_target(own, {"pinned_version": "latest"}) == \
        Target("latest", "latest", False)


# -- the store: asking, answering, forgetting ----------------------------------------

@pytest.fixture
def held():
    """A shared machine on stable with its one slot, set up and held by a1."""
    store = Store(":memory:", max_slots_per_machine=8)
    store.add_node("m1", "op", pinned_version="stable", now=1.0)
    store.set_machine_capacity("m1", 1)
    store.add_account("a1", "sub-1", "a@example.com", slot_quota=1, now=1.0)
    store.add_account("b1", "sub-2", "b@example.com", slot_quota=1, now=1.0)
    store.add_slot("s1", "m1", "slot01", now=1.0)
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": False}], now=time.time())
    slot = store.claim_slot("a1", now=time.time())
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": True,
                                    "provisioned_for": slot["claimed_at"]}], now=time.time())
    assert store.get_slot("s1")["state"] == slots.CLAIMED
    yield store
    store.close()


def test_an_update_moves_the_slot_to_latest_and_asks_the_machine_now(held):
    held.request_claude_update("s1", 100.0, to_version=LATEST, held_by="a1")
    assert held.get_slot("s1")["claude_channel"] == "latest"
    assert held.get_claude_update("s1") == {
        "slot_id": "s1", "requested_at": 100.0, "state": "pending", "to_version": LATEST,
        "detail": "", "updated_at": 100.0}


def test_only_the_holder_can_ask_and_a_refusal_changes_nothing(held):
    with pytest.raises(NotYours):
        held.request_claude_update("s1", 100.0, held_by="b1")
    with pytest.raises(NotYours):
        held.choose_stable("s1", held_by="b1")
    assert held.get_slot("s1")["claude_channel"] is None
    assert held.get_claude_update("s1") is None


def test_nothing_is_asked_under_the_operators_hold(held):
    held.set_pinned_version("m1", "2.1.278")
    for act in (lambda: held.request_claude_update("s1", 100.0, held_by="a1"),
                lambda: held.choose_stable("s1", held_by="a1")):
        with pytest.raises(StoreError, match="held"):
            act()
    assert held.get_slot("s1")["claude_channel"] is None
    assert held.get_claude_update("s1") is None


@pytest.mark.parametrize("state", [slots.CLAIMING, slots.RELEASING])
def test_nothing_is_asked_of_a_slot_being_made_or_wiped(held, state):
    with held._lock:
        held._conn.execute("UPDATE slots SET state = ? WHERE id = 's1'", (state,))
        held._conn.commit()
    with pytest.raises(StoreError):
        held.request_claude_update("s1", 100.0, held_by="a1")
    with pytest.raises(StoreError):
        held.choose_stable("s1", held_by="a1")
    assert held.get_slot("s1")["claude_channel"] is None
    assert held.get_claude_update("s1") is None


def test_the_channel_and_the_ask_land_together_or_not_at_all(held):
    """One transaction: an ask that cannot be filed leaves the channel alone."""
    with held._lock:
        held._conn.execute("CREATE TRIGGER refuse BEFORE INSERT ON claude_updates "
                           "BEGIN SELECT RAISE(ABORT, 'refused'); END")
        held._conn.commit()
    with pytest.raises(sqlite3.Error):
        held.request_claude_update("s1", 100.0, held_by="a1")
    assert held.get_slot("s1")["claude_channel"] is None


def test_back_to_stable_withdraws_an_update_still_waiting(held):
    held.request_claude_update("s1", 100.0, held_by="a1")
    held.choose_stable("s1", held_by="a1")
    assert held.get_slot("s1")["claude_channel"] == "stable"
    assert held.get_claude_update("s1") is None


def test_only_an_answer_to_the_waiting_ask_closes_it(held):
    held.request_claude_update("s1", 100.0, held_by="a1")
    assert not held.record_claude_update("s1", 99.0, "done", LATEST, "", 101.0), "another ask"
    assert not held.record_claude_update("s1", 100.0, "installing", "", "", 101.0)
    assert not held.record_claude_update("s1", True, "done", "", "", 101.0)
    assert not held.record_claude_update("s1", "100", "done", "", "", 101.0)
    assert held.get_claude_update("s1")["state"] == "pending"
    assert held.record_claude_update("s1", 100.0, "failed", "", "disk full", 102.0)
    row = held.get_claude_update("s1")
    assert (row["state"], row["detail"], row["to_version"]) == ("failed", "disk full", "")
    assert not held.record_claude_update("s1", 100.0, "done", LATEST, "", 103.0), \
        "answered once"


def test_a_time_that_is_not_a_number_never_names_a_request(held):
    """True would match a request made at 1.0, as SQL sees it."""
    held.request_claude_update("s1", 1.0, held_by="a1")
    assert not held.record_claude_update("s1", True, "done", LATEST, "", 2.0)
    assert held.get_claude_update("s1")["state"] == "pending"


def test_an_unanswered_ask_fails_in_time_and_an_answered_one_is_forgotten(held):
    held.request_claude_update("s1", 100.0, held_by="a1")
    assert held.expire_claude_updates(100.0 + LOGIN_MAX_AGE_S - 1, LOGIN_MAX_AGE_S) == 0
    assert held.expire_claude_updates(100.0 + LOGIN_MAX_AGE_S + 1, LOGIN_MAX_AGE_S) == 1
    row = held.get_claude_update("s1")
    assert row["state"] == "failed" and row["detail"] == Store.UPDATE_TIMED_OUT
    later = row["updated_at"] + LOGIN_MAX_AGE_S + 1
    assert held.expire_claude_updates(later, LOGIN_MAX_AGE_S) == 1
    assert held.get_claude_update("s1") is None


def test_a_slot_given_back_forgets_its_channel_and_its_update(held):
    held.request_claude_update("s1", 100.0, held_by="a1")
    held.begin_release("s1", held_by="a1")
    held.apply_slot_report("m1", [{"unix_user": "slot01", "present": False}], now=time.time())
    assert held.get_slot("s1")["state"] == slots.FREE
    assert held.get_slot("s1")["claude_channel"] is None, "the next holder starts on the pin"
    assert held.get_claude_update("s1") is None


def test_an_owners_node_takes_the_choice_as_its_own_pin(held):
    held.add_node("own-1", "alice", pinned_version="2.1.200", now=1.0)
    held.add_account("c1", "sub-3", "c@example.com", slot_quota=1, now=1.0)
    held.hold_owner_node("own-1", "c1", now=1.0)
    held.request_claude_update("own-1", 100.0, held_by="c1")
    assert held.get_node("own-1")["pinned_version"] == "latest", "an exact pin is no hold here"
    assert held.get_claude_update("own-1")["state"] == "pending"
    held.choose_stable("own-1", held_by="c1")
    assert held.get_node("own-1")["pinned_version"] == "stable"
    held.request_claude_update("own-1", 200.0, held_by="c1")
    held.unhold_owner_node("own-1")
    assert held.get_claude_update("own-1") is None


# -- what a machine is told ---------------------------------------------------------

def test_a_slot_on_latest_is_told_the_number_and_to_install_now():
    node = {"id": "m1", "pinned_version": "stable"}
    slot = {"id": "s1", "unix_user": "slot01", "state": slots.ACTIVE, "claude_channel": "latest",
            "kind": slots.MACHINE_SLOT}
    pending = {"state": "pending", "requested_at": 100.0}
    desired = desired_state(node, slots=[slot], hostname="erik-1", channels=channels_at(1.0),
                            slot_updates={"s1": pending})
    assert desired["slots"][0] == {"unix_user": "slot01", "state": slots.ACTIVE,
                                   "claude_version": "latest", "channel_version": LATEST,
                                   "update_now": {"requested_at": 100.0}}
    assert desired["claude_version"] == "stable", "the machine's own pin, unchanged"
    assert desired["poll_s"] == LOGIN_POLL_S, "somebody is watching the page"
    answered = desired_state(node, slots=[slot], hostname="erik-1", channels=channels_at(1.0),
                             slot_updates={"s1": {**pending, "state": "done"}})
    assert "update_now" not in answered["slots"][0] and answered["poll_s"] == IDLE_POLL_S


def test_a_held_slot_is_told_the_hold_and_nothing_else():
    node = {"id": "m1", "pinned_version": "2.1.278"}
    slot = {"id": "s1", "unix_user": "slot01", "state": slots.ACTIVE, "claude_channel": "latest",
            "kind": slots.MACHINE_SLOT}
    desired = desired_state(node, slots=[slot], hostname="erik-1", channels=channels_at(1.0),
                            slot_updates={"s1": {"state": "pending", "requested_at": 1.0}})
    assert desired["slots"][0] == {"unix_user": "slot01", "state": slots.ACTIVE,
                                   "claude_version": "2.1.278"}


def test_an_owners_node_is_told_its_number_and_its_update():
    desired = desired_state({"id": "own-1", "pinned_version": "latest"},
                            channels=channels_at(1.0),
                            own_update={"state": "pending", "requested_at": 7.0})
    assert desired["claude_version"] == "latest"
    assert desired["channel_version"] == LATEST
    assert desired["update_now"] == {"requested_at": 7.0}
    assert desired["poll_s"] == LOGIN_POLL_S


def test_a_machine_is_not_told_an_owners_fields():
    desired = desired_state({"id": "m1", "pinned_version": "latest"}, slots=[],
                            hostname="m1", channels=channels_at(1.0),
                            own_update={"state": "pending", "requested_at": 7.0})
    assert "update_now" not in desired and "channel_version" not in desired


# -- what a machine says back ---------------------------------------------------------

def test_a_machine_says_what_it_did_in_one_shape_and_nothing_else_counts():
    payload = _machine_payload("m1", [
        {"unix_user": "slot01", "present": True,
         "claude_update": {"requested_at": 100.0, "state": "done", "to": "2.1.281" + "9" * 50,
                           "detail": ""}},
        {"unix_user": "slot02", "present": True,
         "claude_update": {"requested_at": "100", "state": "done"}},
        {"unix_user": "slot03", "present": True,
         "claude_update": {"requested_at": 100.0, "state": "installing"}}])
    out = validate_heartbeat(payload, "m1")["slots"]
    assert out[0]["claude_update"] == {"requested_at": 100.0, "state": "done",
                                       "to": ("2.1.281" + "9" * 50)[:40], "detail": ""}
    assert "claude_update" not in out[1] and "claude_update" not in out[2]


def test_an_owners_node_says_it_under_its_own_reconcile():
    payload = heartbeat(time.time())["payload"]
    payload["node_id"] = "own-1"
    payload["reconcile"] = {"claude_update": {"requested_at": 7.0, "state": "failed",
                                              "detail": "no space left"}}
    out = validate_heartbeat(payload, "own-1")["reconcile"]["claude_update"]
    assert out == {"requested_at": 7.0, "state": "failed", "to": None,
                   "detail": "no space left"}


def test_a_machine_closes_its_own_slots_update_and_nobody_elses(server):  # noqa: F811
    srv, store = server
    m1 = store.add_node("m1", "op", pinned_version="stable", now=1.0)
    m2 = store.add_node("m2", "op", pinned_version="stable", now=1.0)
    store.add_account("a1", "sub-1", "a@example.com", slot_quota=1, now=1.0)
    for node in ("m1", "m2"):
        store.set_machine_capacity(node, 1)
        store.add_slot(f"{node}-s", node, "slot01", now=1.0)
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": False}], now=time.time())
    slot = store.claim_slot("a1", now=time.time(), node_id="m1")
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": True,
                                    "provisioned_for": slot["claimed_at"]}], now=time.time())
    store.set_channel_versions(channels_at(time.time()), now=time.time())
    store.request_claude_update("m1-s", 100.0, to_version=LATEST, held_by="a1")

    said = {"unix_user": "slot01", "present": True,
            "claude_update": {"requested_at": 100.0, "state": "done", "to": LATEST}}
    reply = json.loads(call(srv, "POST", "/api/heartbeat", _machine_payload("m2", [said]),
                            {"Authorization": f"Bearer {m2}"})[1])
    assert store.get_claude_update("m1-s")["state"] == "pending", "another machine's word"
    reply = json.loads(call(srv, "POST", "/api/heartbeat",
                            _machine_payload("m1", [{**said, "claude_update": None}]),
                            {"Authorization": f"Bearer {m1}"})[1])
    assert reply["desired"]["slots"][0]["update_now"] == {"requested_at": 100.0}
    assert reply["desired"]["slots"][0]["channel_version"] == LATEST
    call(srv, "POST", "/api/heartbeat", _machine_payload("m1", [said]),
         {"Authorization": f"Bearer {m1}"})
    assert store.get_claude_update("m1-s")["state"] == "done"


def test_an_owners_node_closes_its_own_update(server):  # noqa: F811
    srv, store = server
    token = store.add_node("own-1", "alice", pinned_version="stable", now=1.0)
    store.add_account("a1", "sub-1", "a@example.com", slot_quota=1, now=1.0)
    store.hold_owner_node("own-1", "a1", now=1.0)
    store.request_claude_update("own-1", 7.0, held_by="a1")
    payload = heartbeat(time.time())["payload"]
    payload["node_id"] = "own-1"
    reply = json.loads(call(srv, "POST", "/api/heartbeat", payload,
                            {"Authorization": f"Bearer {token}"})[1])
    assert reply["desired"]["update_now"] == {"requested_at": 7.0}
    payload["reconcile"] = {"claude_update": {"requested_at": 7.0, "state": "done",
                                              "to": LATEST}}
    call(srv, "POST", "/api/heartbeat", payload, {"Authorization": f"Bearer {token}"})
    assert store.get_claude_update("own-1")["state"] == "done"


# -- the periodic check ------------------------------------------------------------------

def test_the_periodic_check_reads_the_channels_and_a_heartbeat_never_does(cfg):
    store = Store(":memory:")
    calls = []
    monitor = Monitor(store, cfg, LogNotifier(), channel_fetcher=fetcher(calls=calls))
    store.add_node("n1", "op", now=1.0)
    monitor.record_heartbeat(store.get_node("n1"), heartbeat(5000.0)["payload"], 5000.0)
    assert calls == [], "a node's reply never waits on a download"
    monitor.check_all(5000.0)
    assert store.get_channel_versions() == channels_at(5000.0)
    monitor.check_all(5100.0)
    assert len(calls) == 2, "not again inside the hour"
    Monitor(store, cfg, LogNotifier()).check_all(99999.0)
    assert len(calls) == 2 and store.get_channel_versions() == channels_at(5000.0), \
        "a monitor with no fetcher reads nothing"
    store.close()


def test_the_periodic_check_fails_an_update_nobody_answered(held, cfg):
    held.request_claude_update("s1", 100.0, held_by="a1")
    Monitor(held, cfg, LogNotifier()).check_all(100.0 + LOGIN_MAX_AGE_S + 1)
    assert held.get_claude_update("s1")["state"] == "failed"


# -- the page ----------------------------------------------------------------------------

def on_the_page(store, sign_in, pin="stable", version="2.1.267", **said):
    machine(store, users=("slot01",))
    store.set_pinned_version("m1", pin)
    store.set_channel_versions(channels_at(time.time()), now=time.time())
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    report(store, "m1", [{"unix_user": "slot01", "present": True,
                          "claude": {"version": version}, **said}])
    return erik, slot


def test_a_newer_release_is_offered_with_one_press(site):  # noqa: F811
    store, sign_in, _ = site
    erik, slot = on_the_page(store, sign_in)
    row = cc_row(erik.page())
    assert "2.1.267 · Stable · latest is 2.1.281" in text(row)
    assert ">Update to 2.1.281</button>" in row and 'class="primary"' in row
    assert usersite.UPDATE_NOTE in text(row)
    assert "Back to Stable" not in row

    at = erik.press(f"/account/slots/{slot['id']}/update").getheader("Location")
    assert "note=updating" in at
    assert store.get_slot(slot["id"])["claude_channel"] == "latest"
    assert store.get_claude_update(slot["id"])["to_version"] == LATEST
    page = erik.page()
    assert text(cc_row(page)).startswith("Claude CodeUpdating to 2.1.281…")
    assert "<button" not in cc_row(page), "nothing to press while it happens"
    assert refresh_of(page)[0] == usersite.ACTIVE_REFRESH_S, "and the page comes back soon"


def test_on_latest_and_current_it_says_so_with_a_way_back(site):  # noqa: F811
    store, sign_in, _ = site
    erik, slot = on_the_page(store, sign_in, version=LATEST)
    erik.press(f"/account/slots/{slot['id']}/update")
    store.record_claude_update(slot["id"], store.get_claude_update(slot["id"])["requested_at"],
                               "done", LATEST, "", time.time())
    page = erik.page()
    row = cc_row(page)
    assert "2.1.281 · up to date · follows the latest release" in text(row)
    assert ">Back to Stable</button>" in row and "Update to" not in row
    assert refresh_of(page)[0] == usersite.IDLE_REFRESH_S
    assert "note=stable" in erik.press(f"/account/slots/{slot['id']}/stable").getheader(
        "Location")
    assert store.get_slot(slot["id"])["claude_channel"] == "stable"


def test_installed_while_remote_control_still_runs_the_old_one(site):  # noqa: F811
    store, sign_in, _ = site
    erik, slot = on_the_page(store, sign_in, version=LATEST,
                             upgrade={"ok": True, "to": "latest", "restart": "waiting"})
    erik.press(f"/account/slots/{slot['id']}/update")
    store.record_claude_update(slot["id"], store.get_claude_update(slot["id"])["requested_at"],
                               "done", LATEST, "", time.time())
    assert ("Updated to 2.1.281 · Remote Control switches over once no session is open"
            in text(cc_row(erik.page())))


def test_a_failure_is_said_and_the_button_stays(site):  # noqa: F811
    store, sign_in, _ = site
    erik, slot = on_the_page(store, sign_in)
    erik.press(f"/account/slots/{slot['id']}/update")
    store.record_claude_update(slot["id"], store.get_claude_update(slot["id"])["requested_at"],
                               "failed", "", "<img src=x onerror=alert(1)>", time.time())
    row = cc_row(erik.page())
    assert "Update failed: <img src=x onerror=alert(1)>" in text(row)
    assert "<img" not in row, "a machine's words are shown as text"
    assert ">Update to 2.1.281</button>" in row and ">Back to Stable</button>" in row


def test_a_held_machine_offers_nothing_and_refuses_the_press(site):  # noqa: F811
    store, sign_in, _ = site
    erik, slot = on_the_page(store, sign_in, pin="2.1.278", version="2.1.278")
    row = cc_row(erik.page())
    assert "2.1.278 · held by the operator" in text(row)
    assert "<button" not in row and "<form" not in row
    for action in ("update", "stable"):
        at = erik.press(f"/account/slots/{slot['id']}/{action}").getheader("Location")
        assert "note=not-now" in at
    assert store.get_slot(slot["id"])["claude_channel"] is None
    assert store.get_claude_update(slot["id"]) is None


def test_a_press_without_the_pages_token_changes_nothing(site):  # noqa: F811
    store, sign_in, _ = site
    erik, slot = on_the_page(store, sign_in)
    for action in ("update", "stable"):
        reply = erik.call("POST", f"/account/slots/{slot['id']}/{action}",
                          form={"csrf": "0" * 64})
        assert reply.status == 403
    assert store.get_slot(slot["id"])["claude_channel"] is None
    assert store.get_claude_update(slot["id"]) is None


def test_no_row_until_the_machine_says_a_version_it_could_run(site):  # noqa: F811
    store, sign_in, _ = site
    erik, _ = on_the_page(store, sign_in, version="<script>alert(1)</script>")
    page = erik.page()
    assert cc_row(page) == "" and "<script>alert" not in page


def test_an_unread_channel_claims_nothing(site):  # noqa: F811
    store, sign_in, _ = site
    erik, slot = on_the_page(store, sign_in)
    with store._lock:
        store._conn.execute("DELETE FROM settings WHERE key = ?",
                            (claude_versions.SETTING_KEY,))
        store._conn.commit()
    row = cc_row(erik.page())
    assert text(row) == "Claude Code2.1.267 · Stable"


def test_an_owners_own_node_has_the_row_and_its_pin_moves(site):  # noqa: F811
    store, sign_in, _ = site
    store.add_node("erik-1", "erik", pinned_version="2.1.200", now=time.time())
    store.set_channel_versions(channels_at(time.time()), now=time.time())
    erik = sign_in(quota=1)
    store.hold_owner_node("erik-1", erik.account["id"], now=time.time())
    store.insert_heartbeat("erik-1", time.time(), {
        "node_id": "erik-1", "claude": {"version": "2.1.200"},
        "credentials": {"logged_in": True}, "remote_control": {"state": "active"}})
    row = cc_row(erik.page())
    assert "2.1.200 · pinned · latest is 2.1.281" in text(row), "their pin, not a hold"
    assert ">Update to 2.1.281</button>" in row
    erik.press("/account/slots/erik-1/update")
    assert store.get_node("erik-1")["pinned_version"] == "latest"


# -- the console ----------------------------------------------------------------------

def test_the_console_shows_each_slots_version_and_the_releases(console):  # noqa: F811
    store, call_as = console
    shared(store, users=("slot01",))
    store.set_pinned_version("m1", "stable")
    store.set_channel_versions(channels_at(time.time()), now=time.time())
    store.add_account("a1", "sub-1", "a@example.com", slot_quota=1, now=1.0)
    slot = store.claim_slot("a1", now=time.time())
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": True,
                                    "provisioned_for": slot["claimed_at"]}], now=time.time())
    store.insert_heartbeat("m1", time.time(), {
        "node_id": "m1", "mode": "machine",
        "slots": [{"unix_user": "slot01", "present": True, "claude": {"version": "2.1.267"}}]})
    page = text(call_as("GET", "/admin").body)
    assert "Claude Code releases: Latest 2.1.281 · Stable 2.1.273" in page
    assert "Claude Code 2.1.267 (stable)" in page
    store.request_claude_update(slot["id"], time.time(), held_by="a1")
    assert "Claude Code 2.1.267 (latest)" in text(call_as("GET", "/admin").body)
