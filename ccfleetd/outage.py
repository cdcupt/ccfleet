"""Outage emails (Erik, 2026-09-24): to the holders who asked for them, when
their slot's machine has been down for five minutes, and again when it is back.

Decided under the monitor's lock, once a minute, from the states the status
page counts (ccfleetd/status.py): an email falling due is queued, one per
person, in the same transaction as the change to the outage that owes it, and
sent after the lock. Resend's acceptance is what marks it sent; a
send that fails is tried again on later minutes, MAX_TRIES times at most,
never once it is stale: a "down" not yet sent when the machine is back is
dropped, and so is whatever is owed to somebody who turns the emails off. The
end of an outage is told only to those who heard its start. The site is the
one thing that cannot say it is down, since it is what sends: it says, once it
is back, that it was, and that slots kept working through it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from html import escape
from typing import Any, Optional

from .mail import Email
from .status import RED, State

#: How long a machine is down before its holders hear of it.
DOWN_AFTER_S = 5 * 60
#: Tries at one email before it is given up, a minute or more apart.
MAX_TRIES = 5
#: The kinds of email: a machine down, the same machine back, the site's own outage.
DOWN, BACK, SITE = "down", "back", "site"
#: The site's outages, beside the machines' (whose component is their id).
SITE_COMPONENT = "site"


def when(at: float) -> str:
    """A moment as an email says it: in UTC, since an email cannot know the
    reader's zone."""
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def lasted(seconds: float) -> str:
    minutes = max(1, int(seconds // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes} min"


def _recipients(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Whom to tell, each with the name their slot goes by."""
    return [{"account_id": r["account_id"], "email": r["email"],
             "slot_name": r.get("name") or r.get("slot_id") or ""}
            for r in rows]


def machine_outages(store: Any, machines: Mapping[str, State], now: float) -> None:
    """Each machine's outage as it stands now, and the emails it owes, queued."""
    # News only after five minutes of it seen while the server listened: an
    # outage that began before the server went down may have ended meanwhile,
    # its machine's reports reaching nobody.
    listening = store.listening_since() or 0.0
    for node_id, state in machines.items():
        outage = store.open_outage(node_id)
        if state.level == RED:
            if outage is None:
                outage = store.begin_outage(node_id, state.since if state.since else now)
            if (outage["down_sent_at"] is None
                    and now - max(outage["started_at"], listening) >= DOWN_AFTER_S):
                store.owe_outage_start(outage["id"], DOWN,
                                       _recipients(store.outage_emails_for(node_id)), now)
        elif outage is not None:
            # Told as over only to those who heard it begin (see
            # Store.end_outage): one under five minutes was never news.
            store.end_outage(outage["id"], now, told=BACK)
    # A machine that stopped counting — switched off, taken away — mid-outage
    # has nobody left to tell, so its outage just ends.
    for outage in store.open_outages():
        if outage["component"] not in machines:
            store.end_outage(outage["id"], now)


def site_back(store: Any, last_minute: Optional[int], now: float, grace_minutes: int) -> None:
    """The site's own silence, owed once it is over to everybody who asked for
    outage emails, when it lasted five minutes or more. A restart is shorter."""
    if last_minute is None:
        return
    down_from = (last_minute + 1) * 60
    if now - down_from < DOWN_AFTER_S + grace_minutes * 60:
        return
    store.owe_past_outage(SITE_COMPONENT, down_from, now, SITE)


def _email(to: str, subject: str, lines: list[str], url: str) -> Email:
    """One message, its closing the same for all: where to look, and how to stop."""
    base = url.rstrip("/")
    tail = [f"Status: {base}/status" if base else "",
            "You get this because you asked for outage emails on your page"
            + (f" ({base}/account)" if base else "") + "; turn them off there."]
    text = "\n\n".join(line for line in lines + tail if line)
    html = "".join(f"<p>{escape(line)}</p>" for line in lines + tail if line)
    return Email(to=to, subject=subject, text=text, html=html)


def render(row: Mapping[str, Any], url: str) -> Email:
    """The email one queued row stands for."""
    name, start, end = row["slot_name"], row["started_at"], row["ended_at"]
    if row["kind"] == DOWN:
        return _email(row["email"], f"Your ccfleet slot {name} is down", [
            f"Your slot {name} has been unreachable since {when(start)}.",
            "While it is down, claude.ai/code and the Claude app cannot reach it. What is "
            "on it stays there. We are on it, and will email you when it is back."], url)
    if row["kind"] == BACK:
        return _email(row["email"], f"Your ccfleet slot {name} is back", [
            f"Your slot {name} is reachable again, since {when(end)}, after "
            f"{lasted(end - start)} down.",
            "Remote Control comes back on by itself; a session that was open when it went "
            "down may need starting again."], url)
    return _email(row["email"], "ccfleet's website was down", [
        f"ccfleet's website and account pages were down from {when(start)} to "
        f"{when(end)}, {lasted(end - start)} in all.",
        "Your slot kept working all along: you reach it through claude.ai/code or the "
        "Claude app, not through our website. This could only be said now, since the "
        "website is what sends these emails."], url)


def owed(store: Any, url: str) -> list[tuple[Mapping[str, Any], Email]]:
    """Every email owed and not yet sent, with the row it came from. Each
    carries a key of its own, the same on every try (see mail.Email.key)."""
    return [(row, replace(render(row, url),
                          key=f"ccfleet-outage-{row['outage_id']}-{row['kind']}-"
                              f"{row['account_id']}"))
            for row in store.owed_outage_emails(MAX_TRIES)]
