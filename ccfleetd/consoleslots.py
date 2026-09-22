"""The console's half of slots: machines, the slots on them, and who holds them.

The operator's view, and only the operator's: an owner's console login never
sees it, and every action here checks the role again rather than trusting
that the form was only shown to an admin.

What it deliberately lacks is any way to act *as* one of these people. There
is no sign-in on anybody's behalf, no view of a sign-in in flight, and nothing
that completes a Claude login for a user. Taking a slot back is the most it
can do to somebody, and that is a release, with the wipe it implies.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from html import escape
from typing import Any, Optional

from . import slots as slotstates
from .render import _age
from .store import Store, StoreError

#: How long a claim may sit in "setting up" before the console calls it stuck.
#: Well before the claim timeout gives up on it, so the operator hears first.
STUCK_AFTER_S = 10 * 60

#: A capacity or an allowance: a few ASCII digits. `str.isdigit` takes "²",
#: which `int` then refuses, and an unbounded count overflows SQLite; either
#: would be a crash where the operator should get a sentence.
COUNT_RE = re.compile(r"[0-9]{1,4}")

STATE_TONE = {slotstates.FREE: "disabled", slotstates.CLAIMING: "warn",
              slotstates.CLAIMED: "warn", slotstates.ACTIVE: "ok",
              slotstates.RELEASING: "warn"}


def _form(action: str, csrf: str, label: str, inner: str = "", cls: str = "") -> str:
    return (f'<form class="field" method="post" action="{escape(action)}">'
            f'<input type="hidden" name="csrf" value="{escape(csrf)}">{inner}'
            f'<button class="{cls}" type="submit">{escape(label)}</button></form>')


def _trouble(slot: Mapping[str, Any], alerts: list[Mapping[str, Any]], now: float) -> list[str]:
    """Why this slot needs the operator, in words. Empty when it does not."""
    said = [a["message"] for a in alerts
            if a["node_id"] == slot["node_id"]
            and str(a["rule"]).endswith(f":{slot['unix_user']}")]
    # A claiming slot always has claimed_at: the claim writes both at once.
    if slot["state"] == slotstates.CLAIMING and now - slot["claimed_at"] > STUCK_AFTER_S:
        said.append(f"setting up for {_age(now, slot['claimed_at'])}; given up at "
                    f"{slotstates.CLAIM_TIMEOUT_S // 60} min")
    return said


def section(store: Store, csrf: str, now: float) -> str:
    """The slots card and the accounts card, for an admin's console."""
    accounts = {a["id"]: a for a in store.list_accounts()}
    return _slots_card(store, accounts, csrf, now) + _accounts_card(store, accounts, csrf, now)


def _slots_card(store: Store, accounts: Mapping[str, Mapping[str, Any]], csrf: str,
                now: float) -> str:
    alerts = store.open_alerts()
    blocks = []
    for node in store.list_nodes():
        rows = store.list_slots(node_id=node["id"])
        capacity = int(node["capacity"])
        if not rows and capacity <= 1:
            continue          # an ordinary owner node: nothing about slots to show
        base = f"/actions/machine/{escape(node['id'])}"
        head = (f'<div class="row-line"><div class="row-name">{escape(node["id"])}'
                f'<span class="muted"> · {len(rows)} of {capacity} declared</span></div>'
                '<div class="actions">'
                + _form(f"{base}/capacity", csrf, "Set capacity",
                        '<input type="text" name="count" class="count" inputmode="numeric" '
                        f'value="{capacity}" required>')
                + _form(f"{base}/slot-add", csrf, "Declare a slot",
                        '<input type="text" name="slot_id" placeholder="slot id" size="10" '
                        'required><input type="text" name="unix_user" placeholder="unix user" '
                        'size="10" required>')
                + "</div></div>")
        lines = [head] + [_slot_line(s, accounts, alerts, csrf, now) for s in rows]
        blocks.append("".join(lines))
    body = "".join(blocks) or ('<p class="quiet">No shared machines yet. A machine joins '
                               "once its capacity is raised on the server, where it is "
                               "set up: <code>ccfleetd slot capacity &lt;machine&gt; "
                               "&lt;count&gt;</code>.</p>")
    return ('<h2 id="slots">Slots</h2><div class="card">' + body +
            '<p class="note">Taking a slot back is a release: the machine wipes it, and it is '
            "free again once the machine confirms the Linux user is gone. There is no way here "
            "to sign in as anybody or to finish anybody's Claude sign-in, by design.</p></div>")


def _slot_line(slot: Mapping[str, Any], accounts: Mapping[str, Mapping[str, Any]],
               alerts: list[Mapping[str, Any]], csrf: str, now: float) -> str:
    holder = accounts.get(slot.get("held_by") or "")
    who = escape(str(holder["email"])) if holder else "&mdash;"
    seen = {1: "on machine", 0: "not on machine"}.get(slot.get("present"), "not yet seen")
    claimed = (f" · claimed {escape(_age(now, slot['claimed_at']))} ago"
               if slot.get("claimed_at") else "")
    trouble = "".join(f'<br><span class="bad-text">{escape(t)}</span>'
                      for t in _trouble(slot, alerts, now))
    base = f"/actions/slot/{escape(slot['id'])}"
    buttons = ""
    if slot["state"] in slotstates.RELEASABLE:
        # Typed, not pre-filled: this deletes somebody's work.
        buttons = _form(f"{base}/reclaim", csrf, "Take back",
                        f'<input type="text" name="confirm" placeholder="type {escape(slot["id"])}"'
                        ' size="12" autocomplete="off" required>', "danger")
    elif slot["state"] == slotstates.FREE:
        buttons = _form(f"{base}/remove", csrf, "Remove")
    tone = STATE_TONE.get(slot["state"], "disabled")
    return (f'<div class="row-line"><div class="row-name">{escape(slot["id"])}'
            f'<span class="muted"> · {escape(slot["unix_user"])} · {escape(seen)}{claimed}'
            f"</span> <span class=\"pill {tone}\">{escape(slot['state'])}</span>"
            f" <span class=\"small\">{who}</span>{trouble}</div>"
            f'<div class="actions">{buttons}</div></div>')


def _accounts_card(store: Store, accounts: Mapping[str, Mapping[str, Any]], csrf: str,
                   now: float) -> str:
    lines = []
    for account in sorted(accounts.values(), key=lambda a: str(a["email"])):
        held = store.held_slot_count(account["id"])
        role = " · operator" if account.get("role") == "admin" else ""
        seen = (f"last here {escape(_age(now, account['last_seen_at']))} ago"
                if account.get("last_seen_at") else "never back")
        lines.append(
            f'<div class="row-line"><div class="row-name">{escape(str(account["email"]))}'
            f'<span class="muted">{role} · holds {held} · {seen}</span></div>'
            '<div class="actions">'
            + _form(f"/actions/account/{escape(account['id'])}/allowance", csrf,
                    "Set allowance",
                    '<input type="text" name="count" class="count" inputmode="numeric" '
                    f'value="{int(account.get("slot_quota") or 0)}" required>')
            + "</div></div>")
    body = "".join(lines) or '<p class="quiet">Nobody has signed in yet.</p>'
    return ('<h2 id="accounts">Accounts</h2><div class="card">' + body +
            '<p class="note">An allowance is how many slots somebody may hold; it starts at '
            "zero. Lowering it takes nothing away: they keep what they hold, and only claiming "
            "more stops. Operators are made on the server: "
            "<code>ccfleetd account role &lt;email&gt; admin</code>.</p></div>")


def _count(form: Mapping[str, str]) -> int:
    raw = form.get("count") or ""
    if not COUNT_RE.fullmatch(raw):
        raise StoreError("that needs a whole number from 0 to 9999")
    return int(raw)


def act(store: Store, kind: str, target: str, action: str, form: Mapping[str, str],
        now: float) -> Optional[str]:
    """One of the operator's slot actions. Returns where to send them back to,
    or None when there is no such action. Raises StoreError to refuse."""
    if kind == "machine" and action == "capacity":
        if not store.set_machine_capacity(target, _count(form)):
            raise StoreError(f"no machine {target!r}")
        return "slots"
    if kind == "machine" and action == "slot-add":
        store.add_slot(form.get("slot_id", "").strip(), target,
                       form.get("unix_user", "").strip(), now=now)
        return "slots"
    if kind == "slot" and action == "reclaim":
        if form.get("confirm") != target:
            raise StoreError(f"type the slot's id, {target}, to confirm")
        try:
            # The operator's side: any holder. It is a release like any other,
            # with the same wipe, and the sign-in in flight goes with it.
            store.begin_release(target)
        except slotstates.TransitionError as exc:     # a stale form: already on its way out
            raise StoreError(str(exc)) from exc
        return "slots"
    if kind == "slot" and action == "remove":
        store.remove_slot(target)       # refuses anything but a free slot: nothing to lose
        return "slots"
    if kind == "account" and action == "allowance":
        if not store.set_slot_quota(target, _count(form)):
            raise StoreError("no such account")
        return "accounts"
    return None
