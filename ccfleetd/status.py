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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from typing import Any, Optional

from . import names, resets
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
#:   slot_credentials_missing, slot_token_expired, slot_credential_renewal,
#:   slot_sign_in_required, slot_login_expiring
#:                           private hosted-account maintenance, not machine uptime
#:   quota_high_session, quota_high_week
#:                           the holder's usage limits
#:   account_elsewhere, account_changed
#:                           the holder's account in two places, or another one
#:   remote_control_down     runs only once its holder is signed in, so it cannot
#:                           tell a broken machine from a signed-out account
#:   egress_changed          a new address is news, not an outage
#:   version_mismatch        Claude Code behind its pin still runs
EXCLUDED_RULES = ("no_heartbeat", "credentials_missing", "token_stale", "token_expired",
                  "slot_credentials_missing", "slot_token_expired", "slot_credential_renewal",
                  "slot_sign_in_required", "slot_login_expiring", "slot_native_compatibility",
                  "quota_high_session", "quota_high_week", "account_elsewhere",
                  "account_changed", "remote_control_down", "egress_changed",
                  "version_mismatch")

UNKNOWN = "unknown"


@dataclass(frozen=True)
class HealthCheck:
    """A bounded, machine-only diagnosis; never a claim that inference works."""

    level: str
    reasons: tuple[str, ...]
    observed_at: Optional[float] = None
    metrics: tuple[tuple[str, float], ...] = ()


def _health_number(value: Any, maximum: float = 100) -> Optional[float]:
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if 0 <= value <= maximum and math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def _health_time(value: Any, now: float) -> Optional[float]:
    measured = _health_number(value, 253402300799)
    return measured if measured is not None and 0 < measured <= now + 5 else None


def platform_check(report: Optional[Mapping[str, Any]], now: float) -> HealthCheck:
    """Read the existing kernel observer, with independent freshness validation."""
    if not isinstance(report, Mapping):
        return HealthCheck(UNKNOWN, ("platform_missing",))
    observed = _health_time(report.get("observed_at"), now)
    if observed is None or now - observed > 60 or report.get("state") not in ("ready", "partial"):
        return HealthCheck(UNKNOWN, ("platform_stale" if observed else "platform_missing",),
                           observed)
    values = []
    for group, key, name in (("host", "cpu_pct", "cpu"), ("host", "memory_pct", "memory"),
                             ("host", "io_wait_pct", "io_wait"), ("host", "steal_pct", "steal"),
                             ("storage", "used_pct", "disk"),
                             ("service", "cpu_budget_pct", "service_cpu"),
                             ("service", "memory_pct", "service_memory")):
        section = report.get(group)
        value = _health_number(section.get(key), 1_000_000 if group == "service" else 100) \
            if isinstance(section, Mapping) else None
        if value is not None:
            values.append((name, value))
    limits = {"disk": 85, "io_wait": 20, "steal": 10}
    reasons = tuple(name + "_pressure" for name, value in values
                    if value >= limits.get(name, 90))
    if any(name == "disk" and value >= 95 for name, value in values):
        return HealthCheck(RED, ("disk_critical", *(r for r in reasons if r != "disk_pressure")),
                           observed, tuple(values))
    level = YELLOW if reasons else GREEN if len(values) == 7 else UNKNOWN
    return HealthCheck(level, reasons or (("resources_normal",) if level == GREEN
                                         else ("metrics_missing",)), observed, tuple(values))


def machine_check(latest: Optional[Mapping[str, Any]], alerts: Sequence[Mapping[str, Any]],
                  shared: bool, now: float) -> HealthCheck:
    """Heartbeat is transport evidence, not a resource or provider success test."""
    heard = _health_time((latest or {}).get("ts"), now)
    if heard is None:
        return HealthCheck(UNKNOWN, ("heartbeat_missing",))
    late, down = thresholds(shared)
    if now - heard >= late:
        return HealthCheck(RED if now - heard >= down else YELLOW,
                           ("heartbeat_down" if now - heard >= down else "heartbeat_late",), heard)
    payload = (latest or {}).get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    values = []
    for section, name in (("disk", "disk"), ("mem", "memory")):
        raw = payload.get(section)
        value = _health_number(raw.get("used_pct")) if isinstance(raw, Mapping) else None
        if value is not None:
            values.append((name, value))
    reasons = []
    level = GREEN
    for alert in alerts:
        rule = str(alert.get("rule", "")).split(":", 1)[0]
        problem = rule_state(rule, str(alert.get("level", "")))
        if problem is not None:
            reasons.append("disk_critical" if rule == "disk_high" and problem == RED else rule)
            if problem == RED:
                level = RED
            elif level == GREEN:
                level = YELLOW
    for name, value in values:
        if value >= (85 if name == "disk" else 90):
            reasons.append(name + "_pressure")
            if level == GREEN:
                level = YELLOW
        if name == "disk" and value >= 95:
            reasons.append("disk_critical")
            level = RED
    if not reasons and len(values) != 2:
        level, reasons = UNKNOWN, ["metrics_missing"]
    return HealthCheck(level, tuple(dict.fromkeys(reasons)) or ("resources_normal",),
                       heard, tuple(values))


HEALTH_WORDS = {GREEN: ("Reported checks look normal", "已上报的检查正常"),
                YELLOW: ("Needs attention", "需要留意"), RED: ("Problem detected", "发现异常"),
                UNKNOWN: ("Not enough data", "数据不足")}
HEALTH_REASONS = {
    "platform_missing": ("No platform resource sample is available.", "尚无平台资源采样。"),
    "platform_stale": ("The platform sample is old; current resource health is unknown.",
                       "平台采样已过期，当前资源状态未知。"),
    "metrics_missing": ("Some resource checks are missing; a heartbeat alone is not a health test.",
                        "部分资源检查缺失；收到心跳不等于通过健康检查。"),
    "heartbeat_missing": ("No machine report is available. "
                          "Machine, agent or network may be unavailable.",
                          "尚无主机上报，可能是主机、采集程序或网络不可用。"),
    "heartbeat_late": ("The machine report is late. Check the machine-to-Fleet connection.",
                       "主机上报延迟，请检查主机到 Fleet 的连接。"),
    "heartbeat_down": ("The machine stopped reporting. This does not identify a provider outage.",
                       "主机已停止上报，但这还不能确定是云服务商故障。"),
    "resources_normal": ("The measured resources show no pressure. Model requests are not tested.",
                         "已测资源未见压力；没有因此测试模型请求。"),
    "disk_pressure": ("Storage is running low.", "磁盘可用空间偏少。"),
    "disk_critical": ("Storage is critically full; writes may fail.",
                      "磁盘空间严重不足，写入可能失败。"),
    "memory_pressure": ("Memory use is high; this may contribute to slow replies.",
                        "内存占用偏高，可能影响响应速度。"),
    "cpu_pressure": ("Platform CPU use is high.", "平台 CPU 占用偏高。"),
    "io_wait_pressure": ("The platform is waiting on storage I/O.",
                         "平台等待磁盘 I/O 的时间偏多。"),
    "steal_pressure": ("The VPS is waiting for CPU from its host; check provider capacity.",
                       "VPS 等待宿主机提供 CPU 的时间偏多，请检查服务商资源容量。"),
    "service_cpu_pressure": ("Fleet is close to its CPU budget.", "Fleet 接近自身 CPU 配额。"),
    "service_memory_pressure": ("Fleet is close to its memory budget.", "Fleet 接近自身内存配额。"),
    "disk_high": ("The machine reports a storage warning.", "主机报告磁盘告警。"),
    "claude_missing": ("The machine cannot find its Claude Code installation.",
                       "主机未找到 Claude Code。"),
    "slot_missing": ("A held slot's runtime environment is missing.",
                     "正在使用的槽位运行环境缺失。"),
    "slot_wipe_failed": ("An unused slot needs cleanup.", "未使用的槽位需要清理。"),
    "slot_occupied": ("An unused slot is not ready to be assigned.", "未使用的槽位尚不能分配。"),
    "slot_provision_failed": ("Slot setup needs operator attention.", "槽位设置需要运营方处理。"),
}


def health_html(check: HealthCheck, now: float) -> str:
    """Fixed explanatory copy and sanitized numbers, safe for public or owned views."""
    from .bilingual import label

    level = check.level if check.level in HEALTH_WORDS else UNKNOWN
    metrics = []
    for name, value in check.metrics:
        names_ = {"cpu": ("CPU", "CPU"), "memory": ("Memory", "内存"),
                  "disk": ("Storage", "磁盘"), "service_cpu": ("Fleet CPU", "Fleet CPU"),
                  "io_wait": ("I/O wait", "I/O 等待"), "steal": ("CPU steal", "宿主机 CPU 等待"),
                  "service_memory": ("Fleet memory", "Fleet 内存")}
        number = _health_number(value, 1_000_000)
        if name in names_ and number is not None:
            en, zh = names_[name]
            metrics.append(label(f"{en} {number:.1f}%", f"{zh} {number:.1f}%", inline=True))
    reasons = ' '.join(label(*HEALTH_REASONS[code], inline=True)
                       for code in check.reasons if code in HEALTH_REASONS)
    stamp = _health_time(check.observed_at, now)
    observed = ('<span class="health-observed">' + label("Last report: ", "最近上报：", inline=True)
                + since_html(stamp, now) + '</span>') if stamp is not None else ''
    return ('<div class="health-check" data-health="' + level + '"><strong>'
            '<span class="st-dot ' + level + '" aria-hidden="true"></span> '
            + label(*HEALTH_WORDS[level], inline=True) + '</strong><p>' + reasons + '</p>'
            + ('<p class="health-metrics">' + ' · '.join(metrics) + '</p>' if metrics else '')
            + observed + '</div>')

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


@dataclass(frozen=True)
class MachineDetail:
    """Machine-only status facts; raw alert bodies and holder data stay out.

    The node id remains private to operator rendering and history lookups. Public
    labels use the separately assigned number, never a node or slot name.
    """

    node_id: str
    public_number: Optional[int]
    state: State
    reasons: tuple[str, ...]
    last_report: Optional[float]
    operator_name: Optional[str] = None


def public_machine_number(node: Mapping[str, Any]) -> Optional[int]:
    """A persistent authored public label, with no fallback to private names."""
    value = node.get("public_status_number")
    return value if type(value) is int and value > 0 else None


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


def _machine_reasons(latest: Optional[Mapping[str, Any]], alerts: Sequence[Mapping[str, Any]],
                     shared: bool, now: float,
                     listening_since: Optional[float]) -> list[tuple[State, str]]:
    """The same allowlisted reasons drive both the colour and public explanation."""
    heard = (latest or {}).get("ts")
    if heard is None:
        return [(State(RED), "heartbeat_missing")]
    heard = float(heard)
    if listening_since is not None:
        heard = max(heard, listening_since)
    late, down = thresholds(shared)
    age = now - heard
    reasons: list[tuple[State, str]] = []
    if age >= down:
        reasons.append((State(RED, heard + down), "heartbeat_down"))
    elif age >= late:
        reasons.append((State(YELLOW, heard + late), "heartbeat_late"))
    for alert in alerts:
        rule = str(alert.get("rule", "")).split(":", 1)[0]
        level = rule_state(rule, str(alert.get("level", "")))
        if level is not None:
            code = "disk_critical" if rule == "disk_high" and level == RED else rule
            reasons.append((State(level, alert.get("opened_at")), code))
    return reasons


def machine_state(latest: Optional[Mapping[str, Any]], alerts: Sequence[Mapping[str, Any]],
                  shared: bool, now: float, listening_since: Optional[float] = None) -> State:
    """One machine now, retaining server-restart grace and account exclusions."""
    return _worst([reason for reason, _code in
                   _machine_reasons(latest, alerts, shared, now, listening_since)])


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


def machine_details(store: Any, now: float) -> list[MachineDetail]:
    """One batched snapshot for the same components that drive aggregate status."""
    latest = store.latest_heartbeats()
    listening = listening_for(store)
    by_node: dict[str, list[Mapping[str, Any]]] = {}
    for alert in store.open_alerts():
        by_node.setdefault(alert["node_id"], []).append(alert)
    slot_rows = store.list_slots()
    slots_by_node: dict[str, list[Mapping[str, Any]]] = {}
    for slot in slot_rows:
        slots_by_node.setdefault(slot["node_id"], []).append(slot)
    details = []
    for node, shared in components(store.list_nodes(), slot_rows, latest):
        node_id = node["id"]
        report = latest.get(node_id)
        heard = (report or {}).get("ts")
        last_report = float(heard) if heard is not None else None
        since = listening(node_id)
        reasons = _machine_reasons(report, by_node.get(node_id, ()), shared, now, since)
        codes = tuple(dict.fromkeys(code for _state, code in sorted(
            reasons, key=lambda reason: -_RANK[reason[0].level])))
        if not codes:
            codes = ("restart_waiting" if since is not None and last_report is not None
                     and last_report < since else "reported",)
        machine_slots = slots_by_node[node_id]
        operator_name = names.display(machine_slots[0]) if len(machine_slots) == 1 else node_id
        details.append(MachineDetail(node_id, public_machine_number(node),
                                     _worst([state for state, _code in reasons]),
                                     codes, last_report, operator_name))
    return details


def snapshot(store: Any, now: float) -> dict[str, State]:
    """Every machine that counts, by node id, as it is now."""
    return {detail.node_id: detail.state for detail in machine_details(store, now)}


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
    per_machine_minutes: dict[str, tuple[int, int]] = field(default_factory=dict)

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
                   (sum(d.up for d in each), sum(d.up + d.down for d in each)),
                   {name[len(MACHINE_PREFIX):]: (
                       sum(d.up for d in found.values()),
                       sum(d.up + d.down for d in found.values()))
                    for name, found in by_component.items() if name.startswith(MACHINE_PREFIX)})


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
.health-check{border-left:3px solid var(--off);padding:8px 12px;margin:10px 0;
font-size:13px;overflow-wrap:anywhere}
.health-check[data-health=green]{border-color:var(--ok)}
.health-check[data-health=yellow]{border-color:var(--warn)}
.health-check[data-health=red]{border-color:var(--bad)}
.health-check p{margin:5px 0}.health-observed{color:var(--muted);font-size:12px}
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
