"""The public status page, /status: whether ccfleet is up, now and over 90 days.

Anybody may read it, signed in or not, so it says only what anybody may know.
Never a machine's id or a slot's name: a slot somebody holds is named after
them (Erik, 2026-09-24), so the machines are one group here, counted, never
listed. Each holder sees their own slot's machine on their own page instead
(status.slot_line).
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from typing import Optional

from . import status
from .config import Config
from .usersite import Viewer, _shell

SITE_NAME = "Website and account pages"
MACHINES_NAME = "Slot machines"
BANNERS = {status.GREEN: "All systems operational", status.YELLOW: "Partial outage",
           status.RED: "Major outage"}
#: A day's colour, as the class its square in the bar takes.
DAY_CLASS = {status.GREEN: "g", status.YELLOW: "y", status.RED: "r", status.NO_DATA: "n"}
#: The page comes back for itself as often as the minutes behind it are counted.
REFRESH = '<meta http-equiv="refresh" content="60">'

STATUS_CSS = """
/* The status page: a banner saying it in a line, then each part of the service
   with its last ninety days, a square a day. */
.st-banner{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;padding:18px 22px;
margin:0 0 18px;border:1px solid var(--rule);border-radius:var(--radius);
background:var(--panel)}
.st-banner strong{font-size:19px;font-weight:720;letter-spacing:-.01em}
.st-banner.green{background:var(--ok-bg);border-color:var(--ok-line)}
.st-banner.yellow{background:var(--warn-bg);border-color:var(--warn-line)}
.st-banner.red{background:var(--bad-bg);border-color:var(--bad-line)}
.st-banner .st-dot{width:11px;height:11px}
.st-since{color:var(--muted);font-size:14.5px}
.st-overall{margin-left:auto;color:var(--muted);font-size:14px}
.st-list{padding:2px 22px}
.st-comp{padding:18px 0 16px;border-bottom:1px solid var(--rule-soft)}
.st-comp:last-child{border-bottom:0}
.st-head{display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px;margin:0 0 12px}
.st-head h2{font-size:16.5px;margin:0}
.st-word{margin-left:auto;font-size:14.5px;font-weight:620}
.st-word.green{color:var(--ok)}.st-word.yellow{color:var(--warn)}.st-word.red{color:var(--bad)}
.st-bar{display:flex;gap:2px;height:34px}
.st-bar .d{flex:1 1 0;min-width:0;border-radius:2px;background:var(--rule)}
.st-bar .g{background:var(--ok)}.st-bar .y{background:var(--warn)}.st-bar .r{background:var(--bad)}
.st-scale{display:flex;justify-content:space-between;gap:10px;margin-top:7px;
font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.st-scale .d30{display:none}
.st-key{padding:4px 22px 18px}
.st-key>h2{margin:18px 0 0}
.st-key ul{list-style:none;padding:0;margin:12px 0 16px;display:grid;
grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 22px}
.st-key li{display:flex;align-items:baseline;gap:9px;margin:0;font-size:14.5px}
.st-key .sq{display:inline-block;width:10px;height:10px;border-radius:2px;flex:none;
background:var(--rule);transform:translateY(1px)}
.st-key .sq.g{background:var(--ok)}.st-key .sq.y{background:var(--warn)}
.st-key .sq.r{background:var(--bad)}
@media (max-width:560px){.st-bar{gap:1px;height:30px}
.st-bar .d:nth-child(-n+60){display:none}.st-scale .d90{display:none}
.st-scale .d30{display:inline}.st-overall{margin-left:0;flex-basis:100%}
.st-list,.st-key{padding-left:16px;padding-right:16px}
.st-key ul{grid-template-columns:minmax(0,1fr)}}
"""


def _day_title(day: status.Day, group: bool = False) -> str:
    """What one day was, said on hover: the square's title. For the machines,
    down is every one of them down at once, and degraded some in trouble."""
    if day.colour == status.NO_DATA:
        return f"{day.day} (UTC): no data"
    if group:
        said = [f"every machine down {day.down} min"] if day.down else []
        if day.degraded:
            said.append(f"some in trouble {day.degraded} min")
        return f"{day.day} (UTC): " + (", ".join(said) or "all up")
    said_up = f"{day.day} (UTC): {status.percent(day.up, day.up + day.down)} up"
    return said_up + (f", down {day.down} min" if day.down else "")


def _component(name: str, level: str, word: str, days: Sequence[status.Day],
               minutes: tuple[int, int], group: bool = False) -> str:
    """One part of the service: its state now, and its days as a bar.

    The bar is one picture to a screen reader, its label the uptime and how
    many days had trouble; each square says its own day on hover."""
    up, counted = minutes
    pct = status.percent(up, counted)
    trouble = sum(1 for d in days if d.colour in (status.YELLOW, status.RED))
    label = (f"{name}: {pct} uptime over the last {len(days)} days; "
             f"{trouble} {'day' if trouble == 1 else 'days'} with trouble"
             if counted else f"{name}: no data yet")
    squares = "".join(f'<span class="d {DAY_CLASS[d.colour]}" '
                      f'title="{escape(_day_title(d, group))}"></span>' for d in days)
    return (f'<div class="st-comp"><div class="st-head">'
            f'<span class="st-dot {level}" aria-hidden="true"></span>'
            f"<h2>{escape(name)}</h2>"
            f'<span class="st-word {level}">{escape(word)}</span></div>'
            f'<div class="st-bar" role="img" aria-label="{escape(label)}">{squares}</div>'
            '<div class="st-scale"><span><span class="d90">90 days ago</span>'
            '<span class="d30">30 days ago</span></span>'
            f"<span>{escape(pct + ' uptime' if counted else pct)}</span>"
            "<span>Today</span></div></div>")


def machines_word(states: Sequence[status.State]) -> str:
    """The machines, counted: how many of them are up, and what the rest are."""
    if not states:
        return "No slot machines yet"
    said = [f"{sum(1 for s in states if s.level == status.GREEN)} of {len(states)} "
            "operational"]
    for level, word in ((status.YELLOW, "degraded"), (status.RED, "down")):
        count = sum(1 for s in states if s.level == level)
        if count:
            said.append(f"{count} {word}")
    return " · ".join(said)


KEY = (
    '<div class="card st-key"><h2>Reading this page</h2><ul>'
    '<li><span class="sq g"></span>Up all day</li>'
    '<li><span class="sq y"></span>Degraded, or down for less than 5 minutes</li>'
    '<li><span class="sq r"></span>Down for 5 minutes or more</li>'
    '<li><span class="sq"></span>No data: before counting began</li></ul>'
    f"<p><strong>{SITE_NAME}</strong> is this site: signing in, your page and these "
    "pages. While it is down your slot keeps working, because you reach it through "
    "claude.ai/code or the Claude app, not through this site.</p>"
    f"<p><strong>{MACHINES_NAME}</strong> are the machines slots run on, counted together; "
    "your own page says how yours is. A machine is down when it has not reported for "
    "5 minutes, or a check of the machine itself fails; degraded when it reports late or a "
    "check warns, such as a disk nearly full. Your own sign-in and usage limits are never "
    "counted against it.</p>"
    '<p class="muted small">The squares are UTC days; times are in your own time zone. '
    "This page refreshes itself every minute.</p></div>")


def page(store: object, cfg: Config, viewer: Optional[Viewer], now: float) -> str:
    """The whole page. The site is up whenever it is served, so only the machines
    can say otherwise now; its own downtime shows in its days."""
    states = list(status.snapshot(store, now).values())
    group = status.group_state(states)
    history = status.history(store, now)
    since = (f'<span class="st-since">since {status.since_html(group.since, now)}</span>'
             if group.since is not None else "")
    up, counted = history.overall_minutes
    overall = (f"{status.percent(up, counted)} uptime over the last "
               f"{status.HISTORY_DAYS} days" if counted else "No uptime counted yet")
    body = (
        '<div class="pagehead"><h1>Status</h1>'
        '<p class="sub">Whether ccfleet is up, now and over the last 90 days.</p></div>'
        f'<div class="st-banner {group.level}">'
        f'<span class="st-dot {group.level}" aria-hidden="true"></span>'
        f"<strong>{BANNERS[group.level]}</strong>{since}"
        f'<span class="st-overall">{escape(overall)}</span></div>'
        '<div class="card st-list">'
        + _component(SITE_NAME, status.GREEN, status.WORDS[status.GREEN], history.site,
                     history.site_minutes)
        + _component(MACHINES_NAME, group.level, machines_word(states), history.machines,
                     history.machines_minutes, group=True)
        + "</div>" + KEY)
    return _shell("status", body, REFRESH, STATUS_CSS, viewer=viewer)
