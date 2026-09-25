"""Whether ccfleet is up: each machine green, yellow or red, and the minutes behind it.

The status page and every holder's slot card read this, so it is held to what
Erik asked for (2026-09-24): the state of the MACHINE, never of somebody's
account; a slot's machine down five minutes is red; the site's own silence is
its downtime; ninety days are kept.
"""
from __future__ import annotations

import inspect
import re

import pytest

from ccfleetd import slots, status
from ccfleetd.rules import LEVEL_CRITICAL, LEVEL_WARN

NOW = 1_790_000_040.0            # 2026-09-21 13:34 UTC, 40 s into its minute
MINUTE = int(NOW // 60)
DAY = status.utc_day(MINUTE)


# -- which alert says what about a machine --------------------------------------------------

@pytest.mark.parametrize("rule,level,expected", [
    ("claude_missing", LEVEL_CRITICAL, status.RED),
    ("slot_missing:slot01", LEVEL_CRITICAL, status.RED),
    ("disk_high", LEVEL_CRITICAL, status.RED),
    ("disk_high", LEVEL_WARN, status.YELLOW),
    ("slot_wipe_failed:slot01", LEVEL_CRITICAL, status.YELLOW),
    ("slot_occupied:slot01", LEVEL_CRITICAL, status.YELLOW),
    ("slot_provision_failed:slot01", LEVEL_WARN, status.YELLOW),
])
def test_a_machine_caused_alert_colours_its_machine(rule, level, expected):
    assert status.rule_state(rule, level) == expected


@pytest.mark.parametrize("rule", [*status.EXCLUDED_RULES, "account_elsewhere:slot01",
                                  "account_changed:slot01", "something_new"])
@pytest.mark.parametrize("level", [LEVEL_WARN, LEVEL_CRITICAL])
def test_the_holders_own_and_informational_alerts_colour_nothing(rule, level):
    """A slot signed out, a token gone stale, a week's quota used up, a new
    address or a version behind its pin is not the machine being down."""
    assert status.rule_state(rule, level) is None


def test_every_rule_the_server_raises_is_placed_one_way_or_the_other():
    """A rule added to rules.py must be decided here, not fall through unseen.
    A rule named from a variable ("quota_high_{name}") is matched by its stem."""
    import ccfleetd.rules as rules
    raised = set(re.findall(r'Finding\(\s*f?"([a-z_]+)', inspect.getsource(rules)))
    decided = (set(status.RULE_STATES) | set(status.LEVELED_RULES)
               | set(status.EXCLUDED_RULES))
    undecided = {name for name in raised if name not in decided and not (
        name.endswith("_") and any(d.startswith(name) for d in decided))}
    assert len(raised) >= 15 and not undecided, undecided


# -- how late is late ---------------------------------------------------------------------

def test_a_shared_machine_is_late_after_3_min_and_down_after_5():
    assert status.thresholds(shared=True) == (180, 300)


def test_an_owners_node_reports_every_5_min_so_waits_longer():
    assert status.thresholds(shared=False) == (660, 900)


def beat(age):
    return {"ts": NOW - age}


@pytest.mark.parametrize("age,expected", [
    (30, status.GREEN), (179, status.GREEN), (180, status.YELLOW), (299, status.YELLOW),
    (300, status.RED), (5000, status.RED)])
def test_a_shared_machine_by_the_age_of_its_last_report(age, expected):
    assert status.machine_state(beat(age), (), True, NOW).level == expected


@pytest.mark.parametrize("age,expected", [
    (400, status.GREEN), (659, status.GREEN), (660, status.YELLOW), (900, status.RED)])
def test_an_owners_node_by_the_age_of_its_last_report(age, expected):
    assert status.machine_state(beat(age), (), False, NOW).level == expected


def test_late_and_down_say_when_they_began():
    assert status.machine_state(beat(200), (), True, NOW).since == NOW - 200 + 180
    assert status.machine_state(beat(1000), (), True, NOW).since == NOW - 1000 + 300


def test_silence_while_the_server_was_down_is_not_the_machines():
    """A report sent while the server was down reached nobody: a machine's
    silence counts from when the server began listening again."""
    back = NOW - 60                                  # the server came back a minute ago
    assert status.machine_state(beat(900), (), True, NOW, back).level == status.GREEN
    assert status.machine_state(beat(900), (), True, back + 180, back).level == status.YELLOW
    assert status.machine_state(beat(900), (), True, back + 300, back) == status.State(
        status.RED, back + 300)


def test_a_report_since_the_server_came_back_is_judged_as_ever():
    assert status.machine_state(beat(400), (), True, NOW, NOW - 600).level == status.RED


def test_a_machine_never_heard_from_is_down_since_nobody_knows_when():
    state = status.machine_state(None, (), True, NOW)
    assert state == status.State(status.RED, None)


def alert(rule, level, opened=NOW - 600):
    return {"rule": rule, "level": level, "opened_at": opened}


def test_an_alert_colours_a_fresh_machine_from_when_it_opened():
    state = status.machine_state(beat(20), [alert("disk_high", LEVEL_WARN, NOW - 90)], True, NOW)
    assert state == status.State(status.YELLOW, NOW - 90)


def test_the_worst_reason_wins_and_says_the_earliest_start_at_its_level():
    """Degraded since long ago and down since lately is down since lately."""
    alerts = [alert("disk_high", LEVEL_WARN, NOW - 500),
              alert("slot_missing:slot01", LEVEL_CRITICAL, NOW - 70),
              alert("claude_missing", LEVEL_CRITICAL, NOW - 40)]
    state = status.machine_state(beat(200), alerts, True, NOW)      # late: yellow
    assert state == status.State(status.RED, NOW - 70)


def test_an_alert_without_its_opening_time_still_colours_its_machine():
    state = status.machine_state(beat(20), [{"rule": "claude_missing", "level": "critical"}],
                                 False, NOW)
    assert state == status.State(status.RED, None)
    # Beside one that says when, the one that does not is simply not asked.
    both = [{"rule": "claude_missing", "level": "critical"},
            alert("slot_missing:slot01", LEVEL_CRITICAL, NOW - 70)]
    assert status.machine_state(beat(20), both, False, NOW) == status.State(status.RED, NOW - 70)


def test_the_holders_alerts_leave_a_fresh_machine_green():
    alerts = [alert(r, LEVEL_CRITICAL) for r in
              ("credentials_missing", "token_expired", "quota_high_week", "egress_changed",
               "version_mismatch", "remote_control_down", "no_heartbeat",
               "account_changed:slot01", "account_elsewhere")]
    assert status.machine_state(beat(20), alerts, True, NOW) == status.State(status.GREEN)


# -- which machines count ------------------------------------------------------------------

def node(node_id, enabled=True):
    return {"id": node_id, "enabled": enabled}


def slot(node_id, kind=slots.MACHINE_SLOT):
    return {"id": node_id, "node_id": node_id, "kind": kind}


def test_the_machines_that_count_carry_a_slot_are_on_and_have_reported():
    nodes = [node("pool-1"), node("erik-1"), node("laptop"), node("off", enabled=False),
             node("new"), node("bare")]
    rows = [slot("pool-1"), slot("erik-1", slots.OWNER_SLOT), slot("off"), slot("new")]
    latest = {"pool-1": {"payload": {"mode": "machine"}}, "erik-1": {"payload": {}},
              "laptop": {"payload": {}}, "off": {"payload": {}},
              "bare": {"payload": {"mode": "machine"}}}
    found = {n["id"]: shared for n, shared in status.components(nodes, rows, latest)}
    # A laptop carries no slot; "off" is switched off; "new" never reported;
    # "bare" says it is a machine but has no slot to offer anybody.
    assert found == {"pool-1": True, "erik-1": False}


def test_a_machine_slot_makes_a_shared_machine_even_before_its_first_mode_report():
    found = status.components([node("pool-2")], [slot("pool-2")], {"pool-2": {"payload": {}}})
    assert found == [(node("pool-2"), True)]


# -- the group -----------------------------------------------------------------------------

G, Y, R = (status.State(status.GREEN), status.State(status.YELLOW, 20.0),
           status.State(status.RED, 10.0))


@pytest.mark.parametrize("states,expected", [
    ([], status.GREEN), ([G, G, G], status.GREEN), ([G, Y, G], status.YELLOW),
    ([G, R, G], status.YELLOW), ([R, Y], status.YELLOW), ([R, R], status.RED),
    ([R], status.RED)])
def test_the_group_is_yellow_for_any_trouble_and_red_only_when_every_machine_is_down(
        states, expected):
    assert status.group_state(states).level == expected


def test_the_group_says_when_its_trouble_began():
    assert status.group_state([G, Y, R]).since == 10.0
    assert status.group_state([G, G]).since is None


def test_a_machine_down_since_nobody_knows_when_does_not_hide_when_the_rest_began():
    never = status.State(status.RED)
    assert status.group_state([never, status.State(status.YELLOW, 5.0)]).since == 5.0
    assert status.group_state([never]).since is None


# -- minutes -------------------------------------------------------------------------------

def counted(store, component):
    return {r["day"]: (r["green"], r["yellow"], r["red"])
            for r in store.status_minutes(since_day="2000-01-01") if r["component"] == component}


def test_a_minute_is_counted_once_however_often_it_is_recorded(store):
    for offset in (0, 5, 17):
        store.count_status(status.SITE, status.GREEN, NOW + offset, grace=2)
    assert counted(store, status.SITE) == {DAY: (1, 0, 0)}
    store.count_status(status.SITE, status.GREEN, NOW + 60, grace=2)
    assert counted(store, status.SITE) == {DAY: (2, 0, 0)}


def test_a_state_is_counted_in_its_own_column(store):
    store.count_status("node:pool-1", status.YELLOW, NOW, grace=2)
    store.count_status("node:pool-1", status.RED, NOW + 60, grace=2)
    assert counted(store, "node:pool-1") == {DAY: (0, 1, 1)}


def test_anything_but_the_three_states_is_refused(store):
    """They become column names in SQL: nothing else may reach it."""
    with pytest.raises(ValueError):
        store.count_status(status.SITE, "green; DROP TABLE alerts", NOW, grace=2)
    with pytest.raises(ValueError):
        store.count_status(status.SITE, status.GREEN, NOW, grace=2, gap_state="x = 1 --")
    assert store.status_minutes(since_day="2000-01-01") == []


def test_the_days_asked_for_start_where_asked(store):
    store.count_status(status.SITE, status.GREEN, NOW - 3 * 86400, grace=2)
    store.count_status(status.SITE, status.GREEN, NOW, grace=2)
    assert [r["day"] for r in store.status_minutes(since_day=DAY)] == [DAY]


def test_a_check_a_little_late_is_not_downtime(store):
    """The loop runs every minute plus however long a check takes, so now and
    then a minute gets no count of its own. Within the grace it takes the
    state of the count that closes it."""
    store.count_status(status.SITE, status.GREEN, NOW, grace=2)
    store.count_status(status.SITE, status.GREEN, NOW + 3 * 60, grace=2)   # two missed
    assert counted(store, status.SITE) == {DAY: (4, 0, 0)}


def test_the_sites_own_silence_is_its_downtime(store):
    """Every count proves the server was up; a gap longer than the grace is the
    server not running, and it is counted as down when it comes back."""
    store.count_status(status.SITE, status.GREEN, NOW, grace=2, gap_state=status.RED)
    store.count_status(status.SITE, status.GREEN, NOW + 11 * 60, grace=2,
                       gap_state=status.RED)
    assert counted(store, status.SITE) == {DAY: (2, 0, 10)}


def test_a_machine_unseen_while_the_server_was_down_is_left_out(store):
    """Nobody was looking: those minutes are neither up nor down for it."""
    store.count_status("node:pool-1", status.GREEN, NOW, grace=2)
    store.count_status("node:pool-1", status.GREEN, NOW + 11 * 60, grace=2)
    assert counted(store, "node:pool-1") == {DAY: (2, 0, 0)}


def test_downtime_across_midnight_lands_on_the_days_it_happened(store):
    midnight = (MINUTE // 1440 + 1) * 1440 * 60          # the next UTC midnight
    store.count_status(status.SITE, status.GREEN, midnight - 3 * 60, grace=2,
                       gap_state=status.RED)
    store.count_status(status.SITE, status.GREEN, midnight + 4 * 60, grace=2,
                       gap_state=status.RED)
    before, after = status.utc_day(midnight // 60 - 1), status.utc_day(midnight // 60)
    assert counted(store, status.SITE) == {before: (1, 0, 2), after: (1, 0, 4)}


def test_a_clock_that_went_back_counts_nothing_until_it_catches_up(store):
    store.count_status(status.SITE, status.GREEN, NOW, grace=2)
    store.count_status(status.SITE, status.GREEN, NOW - 600, grace=2)
    assert counted(store, status.SITE) == {DAY: (1, 0, 0)}


def test_ninety_days_are_kept(store):
    first = status.utc_day(MINUTE - 89 * 1440)
    for when in (NOW - 91 * 86400, NOW - 89 * 86400, NOW):
        store.count_status(status.SITE, status.GREEN, when, grace=2)
    removed = store.prune_status_minutes(before_day=first)
    assert removed == 1 and list(counted(store, status.SITE)) == [first, DAY]


# -- recording, as the monitor does once a minute --------------------------------------------

def fleet(store, now=NOW):
    store.add_node("pool-1", "op", now=now - 86400)
    store.add_slot("pool-1", "pool-1", "slot01", now=now - 86400)
    store.add_node("erik-1", "erik", now=now - 86400)
    store.add_account("a1", "sub-a1", "a1@example.com", slot_quota=1, now=now - 86400)
    store.hold_owner_node("erik-1", "a1", unix_user="erik", now=now - 86400)
    store.add_node("laptop", "erik", now=now - 86400)
    store.insert_heartbeat("pool-1", now - 30, {"node_id": "pool-1", "mode": "machine",
                                                 "slots": []})
    store.insert_heartbeat("erik-1", now - 700, {"node_id": "erik-1"})      # late: yellow
    store.insert_heartbeat("laptop", now - 10, {"node_id": "laptop"})


def test_recording_counts_the_site_every_machine_that_counts_and_the_group(store):
    fleet(store)
    status.record(store, NOW, check_interval_s=60)
    rows = {r["component"]: (r["green"], r["yellow"], r["red"])
            for r in store.status_minutes(since_day=DAY)}
    assert rows == {status.SITE: (1, 0, 0), "node:pool-1": (1, 0, 0),
                    "node:erik-1": (0, 1, 0), status.GROUP: (0, 1, 0)}


def test_the_group_is_counted_red_only_while_every_machine_is_down(store):
    fleet(store)
    store.insert_heartbeat("pool-1", NOW - 400, {"node_id": "pool-1", "mode": "machine",
                                                  "slots": []})
    status.record(store, NOW, check_interval_s=60)                 # erik-1 late, pool-1 down
    store.insert_heartbeat("erik-1", NOW - 1000, {"node_id": "erik-1"})
    status.record(store, NOW + 60, check_interval_s=60)            # both down
    assert counted(store, status.GROUP) == {DAY: (0, 1, 1)}


def test_no_machines_no_group(store):
    status.record(store, NOW, check_interval_s=60)
    assert counted(store, status.GROUP) == {}


def test_recording_forgets_what_is_past_ninety_days(store):
    """The site silent a hundred days is ninety days down on the page, and
    nothing older is kept."""
    fleet(store)
    store.count_status(status.SITE, status.GREEN, NOW - 100 * 86400, grace=2)
    status.record(store, NOW, check_interval_s=60)
    days = sorted(r["day"] for r in store.status_minutes(since_day="2000-01-01")
                  if r["component"] == status.SITE)
    assert days[0] == status.utc_day(MINUTE - 89 * 1440) and days[-1] == DAY
    assert len(days) == status.HISTORY_DAYS


def test_the_grace_follows_the_check_interval():
    assert status.grace_minutes(60) == 2
    assert status.grace_minutes(300) == 10
    assert status.grace_minutes(5) == 2


# -- the ninety days, as the page shows them -------------------------------------------------

def test_the_sites_days_run_from_ninety_days_ago_to_today(store):
    store.count_status(status.SITE, status.GREEN, NOW, grace=2)
    days = status.history(store, NOW).site
    assert len(days) == status.HISTORY_DAYS and days[-1].day == DAY
    assert all(d.colour == status.NO_DATA for d in days[:-1])
    assert days[-1].colour == status.GREEN


@pytest.mark.parametrize("down,degraded,expected", [
    (0, 0, status.GREEN), (0, 3, status.YELLOW), (4, 0, status.YELLOW),
    (5, 0, status.RED), (60, 30, status.RED)])
def test_a_day_is_red_from_five_minutes_down(down, degraded, expected):
    assert status.day_colour(up=1000, degraded=degraded, down=down) == expected


def test_a_day_down_from_start_to_finish_is_red_not_empty():
    assert status.day_colour(up=0, degraded=0, down=1440) == status.RED


def test_a_day_nobody_counted_has_no_colour():
    assert status.day_colour(up=0, degraded=0, down=0) == status.NO_DATA


def minutes_of(store, component, states, start=NOW):
    for i, state in enumerate(states):
        store.count_status(component, state, start + 60 * i, grace=2)


def test_every_machine_down_but_never_at_once_is_a_yellow_day(store):
    """Codex, PR #111: a and b each down five minutes, one after the other, is
    never the service down. Each machine's day is red; the group's is not."""
    minutes_of(store, "node:a", [status.RED] * 5 + [status.GREEN] * 5)
    minutes_of(store, "node:b", [status.GREEN] * 5 + [status.RED] * 5)
    minutes_of(store, status.GROUP, [status.YELLOW] * 10)
    assert status.history(store, NOW + 600).machines[-1].colour == status.YELLOW


def test_every_machine_down_at_once_for_five_minutes_is_a_red_day(store):
    minutes_of(store, status.GROUP, [status.RED] * 5)
    assert status.history(store, NOW + 300).machines[-1].colour == status.RED


def test_the_machines_days_are_the_groups_own(store):
    minutes_of(store, "node:a", [status.RED] * 5)                  # no group minute
    history = status.history(store, NOW + 300)
    assert history.machines[-1].colour == status.NO_DATA
    assert history.machines_minutes == (0, 5)


def test_uptime_is_minutes_up_out_of_minutes_counted(store):
    store.count_status(status.SITE, status.GREEN, NOW, grace=2, gap_state=status.RED)
    store.count_status(status.SITE, status.GREEN, NOW + 5 * 60, grace=2, gap_state=status.RED)
    # 2 up, 4 down: the four minutes between.
    assert status.history(store, NOW + 360).site_minutes == (2, 6)


def test_uptime_with_nothing_counted_is_nothing(store):
    history = status.history(store, NOW)
    assert history.site_minutes == history.machines_minutes == history.overall_minutes == (0, 0)


def test_overall_uptime_counts_every_minute_of_every_component(store):
    store.count_status(status.SITE, status.GREEN, NOW, grace=2)
    store.count_status("node:a", status.RED, NOW, grace=2)
    store.count_status("node:b", status.YELLOW, NOW, grace=2)       # degraded is up
    history = status.history(store, NOW + 60)
    assert history.machines_minutes == (1, 2)
    assert history.overall_minutes == (2, 3)


@pytest.mark.parametrize("up,total,shown", [
    (100, 100, "100%"), (99_999, 100_000, "99.99%"), (9_995, 10_000, "99.95%"),
    (1, 2, "50.00%"), (129_599, 129_600, "99.99%"), (0, 0, "no data yet")])
def test_uptime_is_never_rounded_up_to_perfect(up, total, shown):
    assert status.percent(up, total) == shown
