"""When a usage window resets, from Claude Code's words to an instant.

Claude Code's /usage screen says when each window resets in the machine's own
time zone: "1:10am (Asia/Shanghai)", "Sep 26, 7pm (UTC)". Shown as printed, one
machine's zone sat beside another's, and a machine's clock said where somebody
might be. Read into an instant, a page can say it in the viewer's own zone and
the machines can keep their clocks on UTC.

Words this does not recognise are left alone: the page shows them as printed.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# "1:10am (Asia/Shanghai)", "3pm (UTC)", "Sep 26, 7pm (Asia/Shanghai)".
RESET_RE = re.compile(
    r"(?:(?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2}), )?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?P<half>am|pm) "
    r"\((?P<zone>[A-Za-z][A-Za-z0-9_+\-/]{0,63})\)")


def _zone(name: str) -> Optional[ZoneInfo]:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def reset_at(text: object, read_at: float) -> Optional[float]:
    """The instant a window resets, read at ``read_at``; None if the words are
    not ones this knows.

    The time alone is the next time the clock reads it after the reading: a
    window resets after it was read, never before. A date is this year's, or
    next year's when that is the one still ahead, as it is for "Jan 2" read on
    30 December.
    """
    found = RESET_RE.fullmatch(text.strip()) if isinstance(text, str) else None
    if found is None:
        return None
    zone = _zone(found["zone"])
    hour, minute = int(found["hour"]), int(found["minute"] or 0)
    if zone is None or not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + (12 if found["half"] == "pm" else 0)
    read = datetime.fromtimestamp(read_at, zone)
    if found["month"] is None:
        at = read.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if at < read:
            at = (at + timedelta(days=1)).replace(hour=hour, minute=minute)
        return at.timestamp()
    if found["month"] not in MONTHS:
        return None
    month = MONTHS.index(found["month"]) + 1
    try:
        at = datetime(read.year, month, int(found["day"]), hour, minute, tzinfo=zone)
        if at < read - timedelta(days=1):
            at = at.replace(year=read.year + 1)
    except ValueError:
        return None
    return at.timestamp()


def iso(at: float) -> str:
    """An instant as the page carries it, for the viewer's browser to read."""
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def until(now: float, at: float) -> str:
    """How long from now, in words that need no time zone: "in 3h 37m"."""
    left = int(at - now)
    if left < 60:
        return "now"
    days, rest = divmod(left, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"
