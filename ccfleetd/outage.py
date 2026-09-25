"""Outage emails (Erik, 2026-09-24): to the holders who asked for them, when
their slot's machine has been down for five minutes, and again when it is back.

Decided under the monitor's lock, once a minute, from the states the status
page counts (ccfleetd/status.py), and sent after it: each outage is recorded,
so each one is told once, whatever the sending does. The site is the one thing
that cannot say it is down, since it is what sends; it says afterwards, once it
is back, that it was, and that slots kept working through it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from html import escape
from typing import Any, Optional

from . import names
from .mail import Email
from .status import RED, State

#: How long a machine is down before its holders hear of it.
DOWN_AFTER_S = 5 * 60


def when(at: float) -> str:
    """A moment as an email says it: in UTC, since an email cannot know the
    reader's zone."""
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def lasted(seconds: float) -> str:
    minutes = max(1, int(seconds // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes} min"


def _email(to: str, subject: str, lines: list[str], url: str) -> Email:
    """One message, its closing the same for all: where to look, and how to stop."""
    base = url.rstrip("/")
    tail = [f"Status: {base}/status" if base else "",
            "You get this because you asked for outage emails on your page"
            + (f" ({base}/account)" if base else "") + "; turn them off there."]
    text = "\n\n".join(line for line in lines + tail if line)
    html = "".join(f"<p>{escape(line)}</p>" for line in lines + tail if line)
    return Email(to=to, subject=subject, text=text, html=html)


def _down(row: Mapping[str, Any], since: float, url: str) -> Email:
    name = names.display({"id": row["slot_id"], "name": row.get("name")})
    return _email(row["email"], f"Your ccfleet slot {name} is down", [
        f"Your slot {name} has been unreachable since {when(since)}.",
        "While it is down, claude.ai/code and the Claude app cannot reach it. What is on "
        "it stays there. We are on it, and will email you when it is back."], url)


def _back(row: Mapping[str, Any], since: float, now: float, url: str) -> Email:
    name = names.display({"id": row["slot_id"], "name": row.get("name")})
    return _email(row["email"], f"Your ccfleet slot {name} is back", [
        f"Your slot {name} is reachable again, since {when(now)}, after "
        f"{lasted(now - since)} down.",
        "Remote Control comes back on by itself; a session that was open when it went "
        "down may need starting again."], url)


def machine_outages(store: Any, machines: Mapping[str, State], now: float,
                    url: str) -> list[Email]:
    """Each machine's outage as it stands now, and the emails it is owed."""
    owed: list[Email] = []
    for node_id, state in machines.items():
        outage = store.open_outage(node_id)
        if state.level == RED:
            if outage is None:
                outage = store.begin_outage(node_id, state.since if state.since else now)
            if outage["down_sent_at"] is None and now - outage["started_at"] >= DOWN_AFTER_S:
                owed += [_down(row, outage["started_at"], url)
                         for row in store.outage_emails_for(node_id)]
                store.mark_outage(outage["id"], "down_sent_at", now)
        elif outage is not None:
            store.mark_outage(outage["id"], "ended_at", now)
            # Only an outage its holders heard of is told as over: one under
            # five minutes was never news.
            if outage["down_sent_at"] is not None:
                owed += [_back(row, outage["started_at"], now, url)
                         for row in store.outage_emails_for(node_id)]
                store.mark_outage(outage["id"], "back_sent_at", now)
    # A machine that stopped counting — switched off, taken away — mid-outage
    # has nobody left to tell, so its outage just ends.
    for outage in store.open_outages():
        if outage["component"] not in machines:
            store.mark_outage(outage["id"], "ended_at", now)
    return owed


def site_back(store: Any, last_minute: Optional[int], now: float, grace_minutes: int,
              url: str) -> list[Email]:
    """The site's own silence, told once it is over: to everybody who asked for
    outage emails, when it lasted five minutes or more. A restart is shorter."""
    if last_minute is None:
        return []
    down_from = (last_minute + 1) * 60
    if now - down_from < DOWN_AFTER_S + grace_minutes * 60:
        return []
    lines = [f"ccfleet's website and account pages were down from {when(down_from)} to "
             f"{when(now)}, {lasted(now - down_from)} in all.",
             "Your slot kept working all along: you reach it through claude.ai/code or "
             "the Claude app, not through our website. This could only be said now, "
             "since the website is what sends these emails."]
    return [_email(to, "ccfleet's website was down", lines, url)
            for to in store.outage_subscribers()]
