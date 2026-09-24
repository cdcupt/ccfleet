"""Claude Code updates from the page: the agent half (Erik, 2026-09-24).

The server says, for each held slot (or an owner's own node), which Claude Code
to run, the release its channel stands at, and whether somebody pressed
"Update" on their page. The agent installs a new release at the next quiet
moment instead of waiting out its daily re-check, installs at once when asked,
never while its holder is signing in, and says what happened. Remote Control
still moves onto the new version only once no session is open.
"""

# ruff: noqa: F811  (the fixtures below are imported by name and taken as arguments)
from __future__ import annotations

import json

import pytest

from ccfleet_agent import agent, machine
from tests.test_agent import (  # noqa: F401  (fixtures, by name)
    RC_RESTART,
    _a_home_of_its_own,
    _owner_cfg,
    claude_dir,
    installs,
    moving_claude,
    slot_home,
    slot_state,
)
from tests.test_machine import NOW, Fake, _no_real_network, cfg, two_runs  # noqa: F401

LATEST = {"claude_version": "latest", "may_upgrade": True}


def ask(requested_at=500.0):
    return {"requested_at": requested_at}


# -- what the server may say ------------------------------------------------------------

@pytest.mark.parametrize("value", ["2.1.281-beta", "latest", "2.1", "v2.1.281", 2.1, None,
                                   "2.1.281; rm -rf /", "1" * 5 + ".1.1"])
def test_a_release_number_that_is_not_one_is_never_used(value):
    assert agent.channel_number(value) is None


def test_a_release_number_is_taken_as_the_channel_files_spell_it():
    assert agent.channel_number(" 2.1.281 ") == "2.1.281"


@pytest.mark.parametrize("value", [None, {}, {"requested_at": "5"}, {"requested_at": True},
                                   [5], 5])
def test_an_update_that_names_no_time_is_no_update(value):
    assert agent.update_request(value) is None


# -- a slot: a release the server says is out ----------------------------------------------

def test_a_channel_that_moved_on_is_installed_at_the_next_quiet_moment(slot_home):
    """Resolved a minute ago, so the daily re-check would wait a day; the
    server says latest moved on, so it goes now — once for that release."""
    agent.slot_facts(LATEST, moving_claude([], after="2.1.280"), now=1_000.0)
    calls = []
    facts = agent.slot_facts({**LATEST, "channel_version": "2.1.281"},
                             moving_claude(calls, before="2.1.280", after="2.1.281"),
                             now=1_060.0)
    assert [c[2] for c in installs(calls)] == ["latest"]
    assert facts["claude"] == {"version": "2.1.281"}
    calls = []
    agent.slot_facts({**LATEST, "channel_version": "2.1.281"},
                     moving_claude(calls, before="2.1.281", after="2.1.281"), now=1_120.0)
    assert installs(calls) == [], "installed a release it already runs"


def test_a_release_the_installer_did_not_land_on_is_not_tried_every_minute(slot_home):
    agent.slot_facts(LATEST, moving_claude([], after="2.1.280"), now=1_000.0)
    moved = {**LATEST, "channel_version": "2.1.281"}
    agent.slot_facts(moved, moving_claude([], before="2.1.280", after="2.1.280"), now=1_060.0)
    calls = []
    agent.slot_facts(moved, moving_claude(calls, before="2.1.280", after="2.1.280"),
                     now=1_120.0)
    assert installs(calls) == [], "once per release"


def test_the_release_it_runs_is_no_reason_to_install(slot_home):
    agent.slot_facts(LATEST, moving_claude([], after="2.1.281"), now=1_000.0)
    calls = []
    agent.slot_facts({**LATEST, "channel_version": "2.1.281"},
                     moving_claude(calls, before="2.1.281"), now=1_060.0)
    assert installs(calls) == []


def test_a_channel_that_moved_on_waits_while_its_holder_signs_in(slot_home):
    agent.slot_facts(LATEST, moving_claude([], after="2.1.280"), now=1_000.0)
    calls = []
    agent.slot_facts({**LATEST, "may_upgrade": False, "channel_version": "2.1.281"},
                     moving_claude(calls, before="2.1.280"), now=1_060.0)
    assert installs(calls) == []


# -- a slot: "Update" pressed on the page ------------------------------------------------

def test_an_update_asked_for_installs_now_and_says_so_once(slot_home):
    agent.slot_facts(LATEST, moving_claude([], after="2.1.280"), now=1_000.0)
    calls = []
    facts = agent.slot_facts({**LATEST, "update_now": ask()},
                             moving_claude(calls, before="2.1.280", after="2.1.281"),
                             now=1_060.0)
    assert [c[2] for c in installs(calls)] == ["latest"], "checked a minute ago, and asked"
    assert facts["claude_update"] == {"requested_at": 500.0, "state": "done",
                                      "to": "2.1.281", "detail": ""}
    # The server has not heard yet and asks again: answered, not installed again.
    calls = []
    facts = agent.slot_facts({**LATEST, "update_now": ask()},
                             moving_claude(calls, before="2.1.281"), now=1_120.0)
    assert installs(calls) == [] and facts["claude_update"]["state"] == "done"
    # It heard, and stopped asking: nothing more to say.
    facts = agent.slot_facts(LATEST, moving_claude([], before="2.1.281"), now=1_180.0)
    assert "claude_update" not in facts
    assert "claude_update" not in slot_state(slot_home)


def test_an_update_asked_for_waits_while_its_holder_signs_in(slot_home):
    calls = []
    facts = agent.slot_facts({**LATEST, "may_upgrade": False, "update_now": ask()},
                             moving_claude(calls), now=1_000.0)
    assert installs(calls) == [] and "claude_update" not in facts


def test_an_update_that_fails_says_why_and_is_not_held_back(slot_home):
    """A failure an hour ago would hold off the daily re-check; a press is
    somebody asking now, and they are watching for the answer."""
    agent.slot_facts(LATEST, moving_claude([], install_rc=1), now=1_000.0)
    calls = []
    facts = agent.slot_facts({**LATEST, "update_now": ask()},
                             moving_claude(calls, install_rc=1), now=1_060.0)
    assert len(installs(calls)) == 1
    assert facts["claude_update"]["state"] == "failed"
    assert "network down" in facts["claude_update"]["detail"]


def test_an_update_never_cuts_an_open_session_short(slot_home):
    calls = []
    facts = agent.slot_facts({**LATEST, "update_now": ask()},
                             moving_claude(calls, session=0), now=1_000.0)
    assert len(installs(calls)) == 1 and RC_RESTART not in calls
    assert facts["upgrade"]["restart"] == "waiting"
    assert facts["claude_update"]["state"] == "done"


@pytest.mark.parametrize("junk", [{"requested_at": "500"}, {"requested_at": True}, "now", 1])
def test_an_update_that_names_no_request_is_not_one(slot_home, junk):
    agent.slot_facts(LATEST, moving_claude([], after="2.1.280"), now=1_000.0)
    calls = []
    facts = agent.slot_facts({**LATEST, "update_now": junk},
                             moving_claude(calls, before="2.1.280"), now=1_060.0)
    assert installs(calls) == [] and "claude_update" not in facts


# -- the machine agent: checked on the way in, handed on to the slot ------------------------

def test_the_machine_hands_each_slot_its_own_version_and_update(cfg):  # noqa: F811
    fake = Fake(users=["slot01", "slot02"], desired={"claude_version": "stable", "slots": [
        {"unix_user": "slot01", "state": "active", "claude_version": "latest",
         "channel_version": "2.1.281", "update_now": {"requested_at": NOW}},
        {"unix_user": "slot02", "state": "active"}]})
    requests = two_runs(cfg, fake)
    first, _ = requests["slot01"]
    assert first["claude_version"] == "latest" and first["channel_version"] == "2.1.281"
    assert first["update_now"] == {"requested_at": NOW} and first["may_upgrade"] is True
    second, _ = requests["slot02"]
    assert second["claude_version"] == "stable", "no slot version: the machine's pin"
    assert "channel_version" not in second and "update_now" not in second


@pytest.mark.parametrize("entry,kept", [
    ({"claude_version": "--force", "channel_version": "2.1.281",
      "update_now": {"requested_at": NOW}}, {}),
    ({"claude_version": "latest", "channel_version": "2.1.281; rm -rf /",
      "update_now": {"requested_at": "soon"}}, {"claude_version": "latest"}),
    ({"claude_version": "latest", "update_now": {"requested_at": NOW, "extra": "x"}},
     {"claude_version": "latest", "update_now": {"requested_at": NOW}}),
])
def test_the_machine_keeps_only_what_it_checked(entry, kept):
    [slot] = machine.wanted_slots({"slots": [{"unix_user": "slot01", "state": "active",
                                              **entry}]})
    assert {k: v for k, v in slot.items()
            if k in ("claude_version", "channel_version", "update_now")} == kept


def test_a_slot_not_held_is_told_nothing_about_versions():
    [slot] = machine.wanted_slots({"slots": [{"unix_user": "slot01", "state": "releasing",
                                              "claude_version": "latest",
                                              "update_now": {"requested_at": NOW}}]})
    assert "claude_version" not in slot and "update_now" not in slot


def test_a_slot_being_signed_into_is_asked_to_update_but_told_to_wait(cfg):  # noqa: F811
    login = {"requested_at": NOW, "kind": "login"}
    fake = Fake(users=["slot01"], desired={"claude_version": "stable", "slots": [
        {"unix_user": "slot01", "state": "active", "login": login, "claude_version": "latest",
         "update_now": {"requested_at": NOW}}]})
    request, _ = two_runs(cfg, fake)["slot01"]
    assert request["update_now"] == {"requested_at": NOW} and request["may_upgrade"] is False


def test_a_slots_answer_reaches_the_server_and_junk_does_not(cfg):  # noqa: F811
    said = {"requested_at": NOW, "state": "done", "to": "2.1.281", "detail": ""}
    fake = Fake(users=["slot01"], facts={"claude": {"version": "2.1.281"},
                                         "claude_update": said})
    assert machine.slot_report("slot01", {}, cfg, fake.system(), False)["claude_update"] == said
    fake = Fake(users=["slot01"], facts={"claude": {"version": "2.1.281"},
                                         "claude_update": "done, trust me"})
    assert "claude_update" not in machine.slot_report("slot01", {}, cfg, fake.system(), False)


# -- somebody's own node ----------------------------------------------------------------

def owner_run(tmp_path, claude_dir, monkeypatch, desired, state=None,  # noqa: F811
              result=None):
    """One owner run against a server that answers `desired`; what it posted,
    what it asked the installer, and the state it kept."""
    owner_cfg = _owner_cfg(tmp_path, claude_dir)
    posted, asked = [], []
    monkeypatch.setattr(agent, "build_payload", lambda *a, **k: {"node_id": "node-a",
                                                                 "claude": {"version": "2.1.280"}})
    monkeypatch.setattr(agent, "quota_summary", lambda *a, **k: (None, None))
    monkeypatch.setattr(agent, "send_heartbeat", lambda cfg, payload: (
        posted.append(json.loads(json.dumps(payload))) or 200, json.dumps({"desired": desired})))

    def reconcile(pin, installed, state, *a, **kw):
        asked.append((dict(pin), kw.get("asked")))
        return None if result is None else dict(result)
    monkeypatch.setattr(agent, "reconcile_version", reconcile)
    _, _, kept, _ = agent.run_cycle(owner_cfg, state or {})
    return posted, asked, kept


def test_an_owners_node_updates_when_asked_and_says_so_next_time(tmp_path, claude_dir,  # noqa: F811
                                                                  monkeypatch):
    done = {"from": "2.1.280", "to": "2.1.281", "ok": True, "ts": 1.0, "error": None}
    desired = {"claude_version": "latest", "channel_version": "2.1.281",
               "update_now": {"requested_at": 7.0}}
    _, asked, kept = owner_run(tmp_path, claude_dir, monkeypatch, desired, result=done)
    assert asked == [(desired, True)]
    assert kept["claude_update"] == {"requested_at": 7.0, "state": "done", "to": "2.1.281",
                                     "detail": ""}
    posted, asked, kept = owner_run(tmp_path, claude_dir, monkeypatch, {
        "claude_version": "latest"}, state=kept)
    assert posted[0]["reconcile"]["claude_update"]["state"] == "done", "told the server"
    assert asked == [({"claude_version": "latest"}, False)]
    assert "claude_update" not in kept, "and forgot it once the server stopped asking"


def test_an_owners_node_does_not_update_under_a_sign_in(tmp_path, claude_dir,  # noqa: F811
                                                         monkeypatch):
    desired = {"claude_version": "latest", "channel_version": "2.1.281",
               "update_now": {"requested_at": 7.0},
               "login": {"requested_at": 6.0, "email": "", "kind": "login"}}
    monkeypatch.setattr(agent, "reconcile_login", lambda desired, state: (None, dict(state)))
    _, asked, kept = owner_run(tmp_path, claude_dir, monkeypatch, desired)
    [(pin, now)] = asked
    assert now is False and pin["channel_version"] is None, "neither the ask nor the move"
    assert "claude_update" not in kept
