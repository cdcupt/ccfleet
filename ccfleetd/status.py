"""Whether ccfleet is up: each machine green, yellow or red, and its last 90 days.

The public status page (/status) and every holder's slot card say it (Erik,
2026-09-24). It is about the machines and this site, never about somebody's
account: a slot signed out, a token gone stale or a week's quota used up is
its holder's to see on their own page, not an outage. So a machine's state
comes from two things the server already has: how long ago the machine last
reported, and those of its open alerts that say something about the machine
itself (RULE_STATES, LEVELED_RULES). Everything else is left out, on purpose
(EXCLUDED_RULES).

Once a minute the serving loop counts the minute for this site and for each
machine, in the state each is in (record). The site's count is its proof of
life: minutes it went uncounted, beyond a check running a little late, were
minutes it was not running, and are counted as down when it comes back.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from typing import Any, Optional

from . import resets
from . import slots as slotstates
from .render import _age
from .rules import LEVEL_CRITICAL

GREEN, YELLOW, RED = "green", "yellow", "red"
LEVELS = (GREEN, YELLOW, RED)
#: A day nothing was counted on: before tracking began, or a machine not yet there.
NO_DATA = "none"
WORDS = {GREEN: "Operational", YELLOW: "Degraded", RED: "Down"}

#: How often each kind of node reports: a shared machine's agent every minute,
#: an owner's own node every five. The thresholds scale with it (thresholds).
MACHINE_EVERY_S = 60
OWNER_EVERY_S = 5 * 60

#: The rule-to-state table: the alert rules that say something about the
#: MACHINE, and the state each puts it in. A rule is its name before any ":".
RULE_STATES: dict[str, str] = {
    # Claude Code gone from an owner's node: nothing can run there.
    "claude_missing": RED,
    # A held slot's Linux user gone from its machine: its holder cannot work.
    "slot_missing": RED,
    # A slot nobody holds that the machine cannot offer anybody: a wipe that
    # failed, a free slot whose user exists, a claim that failed to set up.
    # Critical for the operator, who must act; nobody's work is stopped.
    "slot_wipe_failed": YELLOW,
    "slot_occupied": YELLOW,
    "slot_provision_failed": YELLOW,
}
#: Rules whose own level says it: a disk nearly full is degraded, full is down.
LEVELED_RULES = ("disk_high",)
#: Deliberately no colour, and why. Each is somebody's own account or plan, or
#: news rather than trouble:
#:   no_heartbeat            the age of the last report is read directly (thresholds)
#:   credentials_missing, token_stale, token_expired
#:                           the holder's sign-in
#:   quota_high_session, quota_high_week
#:                           the holder's usage limits
#:   account_elsewhere, account_changed
#:                           the holder's account in two places, or another one
#:   remote_control_down     runs only once its holder is signed in, so it cannot
#:                           tell a broken machine from a signed-out account
#:   egress_changed          a new address is news, not an outage
#:   version_mismatch        Claude Code behind its pin still runs
EXCLUDED_RULES = ("no_heartbeat", "credentials_missing", "token_stale", "token_expired",
                  "quota_high_session", "quota_high_week", "account_elsewhere",
                  "account_changed", "remote_control_down", "egress_changed",
                  "version_mismatch")

#: The site's own component, beside one "node:<id>" per machine; the prefix
#: keeps a machine that happens to be called "site" apart from it. The
#: machines are also counted as one group, in the state the group is in that
#: minute: only that says whether they were ever all down at once, which a
#: day of each machine's own minutes cannot.
SITE = "site"
GROUP = "machines"
MACHINE_PREFIX = "node:"
#: How much history is kept and shown.
HISTORY_DAYS = 90
#: A day down this long, or longer, is a red day; less is yellow.
DAY_RED_MINUTES = 5

_RANK = {GREEN: 0, YELLOW: 1, RED: 2}


@dataclass(frozen=True)
class State:
    """A machine's, or a group's, state now, and when it began, when that is known."""

    level: str
    since: Optional[float] = None


def rule_state(rule: str, level: str) -> Optional[str]:
    """What one open alert says about its machine; None when nothing."""
    base = rule.split(":", 1)[0]
    if base in LEVELED_RULES:
        return RED if level == LEVEL_CRITICAL else YELLOW
    return RULE_STATES.get(base)


def thresholds(shared: bool) -> tuple[int, int]:
    """(late after, down after), in seconds since the last report.

    A shared machine reports every minute: late after three, down after five.
    An owner's node reports every five: late after two reports missed and a
    minute more, down after three missed."""
    every = MACHINE_EVERY_S if shared else OWNER_EVERY_S
    return 2 * every + 60, max(5 * 60, 3 * every)


def _worst(reasons: Sequence[State]) -> State:
    """The worst of several reasons, since the earliest at that level."""
    if not reasons:
        return State(GREEN)
    level = max((r.level for r in reasons), key=_RANK.__getitem__)
    times = [r.since for r in reasons if r.level == level and r.since is not None]
    return State(level, min(times) if times else None)


def machine_state(latest: Optional[Mapping[str, Any]], alerts: Sequence[Mapping[str, Any]],
                  shared: bool, now: float, listening_since: Optional[float] = None) -> State:
    """One machine now: from its last report, and its alerts about itself.

    A report sent while the server was down reached nobody, so given when the
    server began listening again (listening_for), the machine's silence counts
    from then."""
    heard = (latest or {}).get("ts")
    if heard is None:
        return State(RED)
    heard = float(heard)
    if listening_since is not None:
        heard = max(heard, listening_since)
    late, down = thresholds(shared)
    age = now - heard
    reasons: list[State] = []
    if age >= down:
        reasons.append(State(RED, heard + down))
    elif age >= late:
        reasons.append(State(YELLOW, heard + late))
    for alert in alerts:
        level = rule_state(str(alert.get("rule", "")), str(alert.get("level", "")))
        if level is not None:
            reasons.append(State(level, alert.get("opened_at")))
    return _worst(reasons)


def components(nodes: Sequence[Mapping[str, Any]], slot_rows: Sequence[Mapping[str, Any]],
               latest: Mapping[str, Mapping[str, Any]]) -> list[tuple[Mapping[str, Any], bool]]:
    """The machines the service stands on, each with whether it is shared.

    A machine counts once it is switched on, carries a slot (a shared
    machine's, or somebody's own node counted as their slot) and has reported
    at least once: a box still being set up, a laptop, or a node nobody counts
    as a slot is not the service. Shared, which is to say reporting every
    minute, is a machine's slot; somebody's own node reports every five."""
    kinds: dict[str, set[str]] = {}
    for row in slot_rows:
        kinds.setdefault(row["node_id"], set()).add(row["kind"])
    return [(node, slotstates.MACHINE_SLOT in kinds[node["id"]]) for node in nodes
            if node.get("enabled") and node["id"] in kinds and node["id"] in latest]


def group_state(states: Sequence[State]) -> State:
    """The machines as one: yellow for any trouble, red only when every one is down."""
    trouble = [s for s in states if s.level != GREEN]
    if not trouble:
        return State(GREEN)
    level = RED if all(s.level == RED for s in states) else YELLOW
    times = [s.since for s in trouble if s.since is not None]
    return State(level, min(times) if times else None)


def listening_for(store: Any) -> Callable[[str], Optional[float]]:
    """Since when each machine's silence counts (see machine_state): from when
    the server began listening this time, but not for a machine whose outage
    was open already. That one stays down until it reports, rather than looking
    fine for a few minutes after every restart, which would tell its holders it
    was back."""
    since = store.listening_since()
    already = {outage["component"] for outage in store.open_outages()}
    return lambda node_id: None if node_id in already else since


def snapshot(store: Any, now: float) -> dict[str, State]:
    """Every machine that counts, by node id, as it is now."""
    latest = store.latest_heartbeats()
    listening = listening_for(store)
    by_node: dict[str, list[Mapping[str, Any]]] = {}
    for alert in store.open_alerts():
        by_node.setdefault(alert["node_id"], []).append(alert)
    return {node["id"]: machine_state(latest.get(node["id"]), by_node.get(node["id"], ()),
                                      shared, now, listening(node["id"]))
            for node, shared in components(store.list_nodes(), store.list_slots(), latest)}


# -- minutes and days ---------------------------------------------------------------------

def utc_day(minute: int) -> str:
    """The UTC date a minute since the epoch falls on, as YYYY-MM-DD."""
    return datetime.fromtimestamp(minute * 60, timezone.utc).strftime("%Y-%m-%d")


def utc_iso(at: float) -> str:
    """An instant as the page carries it, for the viewer's browser to read."""
    return resets.iso(at)


def grace_minutes(check_interval_s: int) -> int:
    """Minutes a count may go missing because a check ran late: twice the
    check interval, and never under two."""
    return max(2, math.ceil(2 * check_interval_s / 60))


def record(store: Any, now: float, check_interval_s: int) -> dict[str, State]:
    """Count this minute: up for the site, since this runs; each machine as it
    is, and the machines as a group. And forget what is older than the
    history kept."""
    grace = grace_minutes(check_interval_s)
    machines = snapshot(store, now)
    store.count_status(SITE, GREEN, now, grace=grace, gap_state=RED)
    for node_id, state in machines.items():
        store.count_status(MACHINE_PREFIX + node_id, state.level, now, grace=grace)
    if machines:
        store.count_status(GROUP, group_state(list(machines.values())).level, now,
                           grace=grace)
    store.prune_status_minutes(before_day=utc_day(int(now // 60) - (HISTORY_DAYS - 1) * 1440))
    return machines


@dataclass(frozen=True)
class Day:
    """One UTC day of a component: minutes up (degraded among them) and down."""

    day: str
    up: int = 0
    degraded: int = 0
    down: int = 0

    @property
    def colour(self) -> str:
        return day_colour(self.up, self.degraded, self.down)


def day_colour(up: int, degraded: int, down: int) -> str:
    """Red from DAY_RED_MINUTES down; yellow for less, or for any degraded;
    green otherwise; no colour when nothing was counted."""
    if up + down == 0:
        return NO_DATA
    if down >= DAY_RED_MINUTES:
        return RED
    return YELLOW if down or degraded else GREEN


@dataclass(frozen=True)
class History:
    """The last HISTORY_DAYS days, oldest first, and the minutes behind the
    uptime of each: (up, counted).

    The machines' days are the group's own minutes: down only while every
    machine was down at once, degraded while some were in trouble. Their
    uptime is every machine's minutes together, so one of three down for an
    hour costs an hour of one machine, not of the service."""

    site: list[Day]
    machines: list[Day]
    site_minutes: tuple[int, int]
    machines_minutes: tuple[int, int]

    @property
    def overall_minutes(self) -> tuple[int, int]:
        """Every minute of every component, the site's and each machine's."""
        return (self.site_minutes[0] + self.machines_minutes[0],
                self.site_minutes[1] + self.machines_minutes[1])


def _day_of(row: Mapping[str, Any]) -> Day:
    return Day(row["day"], up=row["green"] + row["yellow"], degraded=row["yellow"],
               down=row["red"])


def history(store: Any, now: float, days: int = HISTORY_DAYS) -> History:
    """The site's days and the machines' days, from the counted minutes."""
    today = int(now // 60) // 1440
    window = [utc_day((today - back) * 1440) for back in range(days - 1, -1, -1)]
    rows = store.status_minutes(since_day=window[0])
    by_component: dict[str, dict[str, Day]] = {}
    for row in rows:
        by_component.setdefault(row["component"], {})[row["day"]] = _day_of(row)
    site = [by_component.get(SITE, {}).get(day, Day(day)) for day in window]
    machines = [by_component.get(GROUP, {}).get(day, Day(day)) for day in window]
    each = [d for name, found in by_component.items() if name.startswith(MACHINE_PREFIX)
            for d in found.values()]
    return History(site, machines,
                   (sum(d.up for d in site), sum(d.up + d.down for d in site)),
                   (sum(d.up for d in each), sum(d.up + d.down for d in each)))


def percent(up: int, counted: int) -> str:
    """Uptime as people read it, from whole minutes and never rounded up: a
    single minute down never shows as 100%."""
    if counted <= 0:
        return "no data yet"
    if up >= counted:
        return "100%"
    hundredths = up * 10_000 // counted
    return f"{hundredths // 100}.{hundredths % 100:02d}%"


# -- a machine's state, in a line --------------------------------------------------------

#: A state as a dot and a word: on the status page, and on each slot card, whose
#: pages carry these rules with the rest of the user site's.
LINE_CSS = """
/* A machine's state: a dot in its colour, beside the words. */
.st-dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--off);
flex:none}
.st-dot.green{background:var(--ok)}.st-dot.yellow{background:var(--warn)}
.st-dot.red{background:var(--bad)}
.machine-line{display:flex;align-items:center;flex-wrap:wrap;gap:4px 8px;margin:0 0 12px;
font-size:14px}
"""
LINE_WORDS = {GREEN: "operational", YELLOW: "degraded", RED: "down"}


def since_html(since: float, now: float) -> str:
    """When something began, for the viewer's browser to say in their own zone,
    and how long ago for a page without scripts."""
    return (f'<time datetime="{escape(utc_iso(since))}" data-local>'
            f"{escape(_age(now, since))} ago</time>")


def slot_line(state: State, now: float) -> str:
    """A slot card's line about the machine its slot is on, linked to /status."""
    # Only trouble has a beginning: green never carries one.
    since = f" since {since_html(state.since, now)}" if state.since is not None else ""
    return (f'<p class="machine-line"><span class="st-dot {state.level}" aria-hidden="true">'
            f"</span><span>Machine: {LINE_WORDS[state.level]}{since}</span>"
            '<a href="/status">Status page</a></p>')
