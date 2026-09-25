"""The console's half of slots: machines, the slots on them, who holds them,
and what they paid.

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
from dataclasses import dataclass
from html import escape
from typing import Any, Optional

from . import claude_versions, names, payments, plans, pricing
from . import slots as slotstates
from .desired import is_channel, machine_hostname
from .render import _age
from .store import Store, StoreError
from .usersite import STATE_WORDS

#: How long a claim may sit in "setting up" before the console calls it stuck.
#: Well before the claim timeout gives up on it, so the operator hears first.
STUCK_AFTER_S = 10 * 60

#: A capacity or an allowance: a few ASCII digits. `str.isdigit` takes "²",
#: which `int` then refuses, and an unbounded count overflows SQLite; either
#: would be a crash where the operator should get a sentence.
COUNT_RE = re.compile(r"[0-9]{1,4}")
#: A payment's id, from the path. Bounded for the same reason as a count.
PAYMENT_ID_RE = re.compile(r"[0-9]{1,15}")
#: What the price is the price of, in its action path: /actions/price/slot/set.
PRICE_TARGET = "slot"

# A slot's state in the words and colours its holder's own page uses. Free is
# the one state no holder sees there, and an owner's own node, which never
# goes through the lifecycle, is signed in or not.
FREE_WORDS = ("disabled", "Free")
NOT_SIGNED_IN = ("warn", "Not signed in")
#: The states in which a slot's Claude Code is somebody's, so where it is
#: going is worth a word.
IN_USE = (slotstates.CLAIMED, slotstates.ACTIVE)
#: An owner's own node raises its account alert bare, since the node is the
#: slot; a machine names each slot's alert by the slot's Linux user.
OWN_NODE_RULES = ("account_elsewhere",)
#: What an owner's node says of itself at the top of its heartbeat that its
#: row reads, as its owner's page reads it.
OWN_REPORT = ("claude", "credentials")
#: The release channels, newest first.
RELEASE_ORDER = ("latest", "stable")
#: The column heads, for the eye only: each cell also says what it is, to a
#: screen reader, and on a phone, where the rows stack as cards, to everybody.
SLOT_HEAD = ('<div class="slothead" aria-hidden="true"><span>Slot</span><span>Holder</span>'
             "<span>State</span><span>Claude Code</span><span>Held</span>"
             "<span>Attention</span></div>")
EMPTY_SAYS = "No slot declared yet: nobody can claim it until it has one."
OWN_NOTE = ("Counted as their slot, and never wiped or handed out from here. To stop "
            "counting it, on the server: <code>ccfleetd node hold {node} --none</code>")
WIPE_NOTE = ("Take back is a wipe: everything on the slot goes, and it is free again once the "
             "machine confirms.")


def _form(action: str, csrf: str, label: str, inner: str = "", cls: str = "") -> str:
    return (f'<form class="field" method="post" action="{escape(action)}">'
            f'<input type="hidden" name="csrf" value="{escape(csrf)}">{inner}'
            f'<button class="{cls}" type="submit">{escape(label)}</button></form>')


def section(store: Store, csrf: str, now: float) -> str:
    """The slots card, the accounts card and the price, for an admin's console."""
    accounts = {a["id"]: a for a in store.list_accounts()}
    return (_slots_card(store, accounts, csrf, now)
            + _accounts_card(store, accounts, csrf, now, _plans_by_holder(store))
            + _price_card(store, csrf, now))


def _report_of(slot: Mapping[str, Any], payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """What a slot last said about itself: an own node at the top of its
    heartbeat, a machine's slot in its entry under its Linux user."""
    if slot.get("kind") == slotstates.OWNER_SLOT:
        return payload
    return next((r for r in payload.get("slots") or []
                 if isinstance(r, Mapping) and r.get("unix_user") == slot.get("unix_user")), {})


def _plan_of(report: Mapping[str, Any]) -> Optional[str]:
    """The Claude plan of the account signed in on a slot, as it is sold."""
    creds = report.get("credentials") or {}
    return plans.label(creds.get("subscription_type"), creds.get("plan"))


def _plans_by_holder(store: Store) -> dict[Optional[str], list[str]]:
    """Each person's Claude plans, one for each slot they hold that says one.
    A free slot has no sign-in on it, so it says none."""
    latest = store.latest_heartbeats()
    found: dict[Optional[str], list[str]] = {}
    for slot in store.list_slots():
        payload = (latest.get(slot["node_id"]) or {}).get("payload") or {}
        plan = _plan_of(_report_of(slot, payload))
        if plan:
            found.setdefault(slot.get("held_by"), []).append(plan)
    return {who: sorted(said) for who, said in found.items()}


def _price_card(store: Store, csrf: str, now: float) -> str:
    """What the public pages say a slot costs, and the form that changes it."""
    current = store.get_price()
    if current is not None:
        price = current["price"]
        said = (f"{escape(pricing.per_slot(price))}"
                f'<span class="muted"> · set by {escape(str(current["updated_by"]))}, '
                f"{escape(_age(now, current['updated_at']))} ago</span>")
        amount, chosen = price.amount, price.currency
    else:
        said = ('No price set<span class="muted"> · the public pages say price and payment '
                "are agreed with you</span>")
        amount, chosen = "", pricing.CURRENCIES[0]
    options = "".join(f'<option value="{code}"{" selected" if code == chosen else ""}>{code}'
                      "</option>" for code in pricing.CURRENCIES)
    base = f"/actions/price/{PRICE_TARGET}"
    form = _form(f"{base}/set", csrf, "Save",
                 '<input type="text" name="amount" class="price" inputmode="decimal" '
                 f'value="{escape(amount)}" placeholder="20" size="8" '
                 'aria-label="Price per slot per month" required>'
                 f'<select name="currency" aria-label="Currency">{options}</select>',
                 "primary")
    clear = _form(f"{base}/clear", csrf, "Clear") if current is not None else ""
    return ('<h2 id="price">Price</h2><div class="card">'
            f'<div class="row-line"><div class="row-name">{said}</div>'
            f'<div class="actions">{form}{clear}</div></div>'
            '<p class="note">The price of a slot for a month, shown on the public pages. '
            "It is shown, never charged: people pay you directly, an allowance is still what "
            "lets somebody claim a slot, and the payments above are a record.</p></div>")


# -- the slots card -------------------------------------------------------------------

@dataclass(frozen=True)
class _Look:
    """What every row of the card reads, the same for all of them."""

    accounts: Mapping[str, Mapping[str, Any]]
    alerts: list[Mapping[str, Any]]
    channels: Mapping[str, Any]
    csrf: str
    now: float
    #: When the serving loop began listening: a claim is not stuck for time
    #: the server was down (see status.machine_state).
    listening: Optional[float] = None


@dataclass(frozen=True)
class _Machine:
    """One machine: its record, what it last said, the slots declared on it,
    and what is wrong with the whole machine rather than with one slot."""

    node: Mapping[str, Any]
    said: Mapping[str, Any]
    rows: tuple[Mapping[str, Any], ...]
    pills: tuple[str, ...]
    notes: tuple[str, ...]


def _slots_card(store: Store, accounts: Mapping[str, Mapping[str, Any]], csrf: str,
                now: float) -> str:
    """A row a machine, its slot being the row; each row's actions wait under
    its Manage, so nothing that wipes somebody sits out in the open."""
    look = _Look(accounts, store.open_alerts(), store.get_channel_versions(), csrf, now,
                 store.listening_since())
    latest = store.latest_heartbeats()
    rows = "".join(_machine_rows(store, node, (latest.get(node["id"]) or {}).get("payload") or {},
                                 look)
                   for node in store.list_nodes())
    body = SLOT_HEAD + rows if rows else (
        '<p class="quiet">No shared machines yet. A machine joins with its one slot, declared '
        "on the server: <code>ccfleetd slot add &lt;machine&gt; --machine &lt;machine&gt; "
        "--unix-user slot01</code>.</p>")
    return ('<h2 id="slots">Slots</h2><div class="card slots">'
            + _releases(look.channels, now) + body + f'<p class="note">{WIPE_NOTE}</p></div>')


def _machine_rows(store: Store, node: Mapping[str, Any], said: Mapping[str, Any],
                  look: _Look) -> str:
    """One machine's row: its slot's, one saying it has none yet, or nothing
    at all for an ordinary owner node."""
    all_rows = store.list_slots(node_id=node["id"])
    rows = tuple(r for r in all_rows if r["kind"] == slotstates.MACHINE_SLOT)
    owned = [r for r in all_rows if r["kind"] == slotstates.OWNER_SLOT]
    if owned and not rows:
        return _own_row(owned[0], _machine(node, said, rows, shared=False),
                        store.get_claude_update(owned[0]["id"]), look)
    # A machine is one with its slot declared, or one whose agent says it
    # is one — with one slot per machine, capacity no longer tells.
    if not rows and int(node["capacity"]) <= 1 and said.get("mode") != slotstates.MACHINE_MODE:
        return ""          # an ordinary owner node: nothing about slots to show
    machine = _machine(node, said, rows, shared=True)
    if not rows:
        return _empty_row(machine, look)
    # What the machine last said about each slot, by its Linux user.
    reports = {r.get("unix_user"): r for r in said.get("slots") or [] if isinstance(r, Mapping)}
    # From before one slot per machine there may be several: a row each, so
    # none of them hides, each flagged until the extra ones are taken off.
    return "".join(_slot_row(s, machine, reports.get(s["unix_user"]) or {},
                             store.get_claude_update(s["id"]), look) for s in rows)


def _machine(node: Mapping[str, Any], said: Mapping[str, Any],
             rows: tuple[Mapping[str, Any], ...], *, shared: bool) -> _Machine:
    pills, notes = _machine_problems(node, rows, said, shared=shared)
    return _Machine(node, said, rows, tuple(pills), tuple(notes))


def _slot_row(slot: Mapping[str, Any], machine: _Machine, report: Mapping[str, Any],
              update: Optional[Mapping[str, Any]], look: _Look) -> str:
    node = machine.node
    said = _update_said(slot, node, report, update, look.channels)
    pills, notes = _problems(slot, report, look.alerts, said, look.now, look.listening)
    shown = names.display(slot)
    sub = _on_machine(slot, node)
    keeper = _kept_for(node, look.accounts)
    cells = _cells(_name(shown, sub + ([keeper] if keeper else [])),
                   _holder(slot, look.accounts, report), _state_pill(slot),
                   _claude_cell(slot, node, report, said), _held_for(slot, look.now),
                   pills + list(machine.pills))
    crowded = len(machine.rows) > slotstates.MAX_SLOTS_PER_MACHINE
    manage = (_slot_actions(slot, look.csrf) + _keep_part(node, look.csrf)
              + (_advanced_part(machine, look.csrf) if crowded else ""))
    return _row(node["id"], slot["state"], shown, cells, notes + list(machine.notes), manage)


def _own_row(slot: Mapping[str, Any], machine: _Machine,
             update: Optional[Mapping[str, Any]], look: _Look) -> str:
    """Somebody's own node, counted as their slot. Nothing here acts on it:
    ccfleet never wipes, hands out or provisions anything on an owner's node."""
    node = machine.node
    # It says these at the top of its heartbeat, not per slot: read as the
    # owner's own page reads them.
    report = {key: machine.said.get(key) or {} for key in OWN_REPORT}
    said = _update_said(slot, node, report, update, look.channels)
    pills, notes = _problems(slot, report, look.alerts, said, look.now, look.listening)
    shown = names.display(slot)
    # Always somebody's, so said by its name alone, like a held slot.
    cells = _cells(_name(shown, ["own machine"]), _holder(slot, look.accounts, report),
                   _state_pill(slot, report),
                   _claude_cell(slot, node, report, said), _held_for(slot, look.now),
                   pills + list(machine.pills))
    manage = _part("Their own machine", "", OWN_NOTE.format(node=escape(node["id"])))
    return _row(node["id"], slot["state"], shown, cells, notes + list(machine.notes), manage)


def _empty_row(machine: _Machine, look: _Look) -> str:
    """A machine with no slot declared: nobody can claim it, so its Manage
    starts open on declaring one."""
    node = machine.node
    keeper = _kept_for(node, look.accounts)
    cells = (f'<div class="c-name">{_name(node["id"], [keeper] if keeper else [])}</div>'
             f'<div class="c-empty">{EMPTY_SAYS}</div>'
             f'<div class="c-flags">{" ".join(machine.pills)}</div>')
    manage = (_advanced_part(machine, look.csrf)
              + (_keep_part(node, look.csrf) if node.get("reserved_for") else ""))
    return _row(node["id"], "", node["id"], cells, list(machine.notes), manage, opened=True)


def _row(machine_id: str, state: str, shown: str, cells: str, notes: list[str], manage: str,
         *, opened: bool = False) -> str:
    """A row: its cells, a line for each thing it has more to say about, and
    its actions folded under Manage — a details element, so no script."""
    said = f'<ul class="slot-notes">{"".join(notes)}</ul>' if notes else ""
    return (f'<div class="slotrow" data-machine="{escape(machine_id)}" '
            f'data-state="{escape(state)}"><div class="slotline">{cells}</div>{said}'
            f'<details class="manage"{" open" if opened else ""}><summary>Manage'
            f'<span class="vh"> {escape(shown)}</span> '
            '<span class="caret" aria-hidden="true">&#9662;</span></summary>'
            f'<div class="manage-panel">{manage}</div></details></div>')


def _cells(name: str, holder: str, state: str, claude: str, held: str,
           pills: list[str]) -> str:
    """The row's cells, in the order of the column heads."""
    cc = "c-cc" if claude else "c-cc none"
    return (f'<div class="c-name">{name}</div><div class="c-holder">{holder}</div>'
            f'<div class="c-state">{state}</div><div class="{cc}">{claude}</div>'
            f'<div class="c-age">{held}</div><div class="c-flags">{" ".join(pills)}</div>')


def _on_machine(slot: Mapping[str, Any], node: Mapping[str, Any]) -> list[str]:
    """The machine a slot is on, when its name does not say so already.

    A held slot's name is said alone, from the claim until the wipe that
    frees it: not the machine it is on (Erik, 2026-09-24; see
    render._called). Escaped."""
    if slot.get("name") or names.display(slot) == node["id"]:
        return []
    return [f"on {escape(node['id'])}"]


def _name(shown: str, sub: list[str]) -> str:
    """The name its holder and claude.ai know it by, escaped here; under it,
    small, where it is and who it is kept for, which come escaped."""
    under = f'<span class="sub">{" &middot; ".join(sub)}</span>' if sub else ""
    return f'<span class="row-name">{escape(shown)}</span>{under}'


def _holder(slot: Mapping[str, Any], accounts: Mapping[str, Mapping[str, Any]],
            report: Mapping[str, Any]) -> str:
    """Who holds it, and under them the plan of the Claude account on it."""
    holder = accounts.get(slot.get("held_by") or "")
    if holder:
        plan = _plan_of(report)
        under = f'<span class="sub">{escape(plan)}</span>' if plan else ""
        return escape(str(holder["email"])) + under
    return '<span class="muted">free</span>' if slot["state"] == slotstates.FREE else "&mdash;"


def _state_pill(slot: Mapping[str, Any], report: Optional[Mapping[str, Any]] = None) -> str:
    """The state in the words and colours the holder's own page uses. An
    owner's own node never goes through the lifecycle: it is signed in or
    not, by the node's own word."""
    state = str(slot["state"])
    if slot.get("kind") == slotstates.OWNER_SLOT:
        creds = (report or {}).get("credentials")
        signed_in = isinstance(creds, Mapping) and creds.get("logged_in") is True
        tone, words = STATE_WORDS[slotstates.ACTIVE][:2] if signed_in else NOT_SIGNED_IN
    else:
        tone, words = (STATE_WORDS[state][:2] if state in STATE_WORDS
                       else FREE_WORDS if state == slotstates.FREE else ("disabled", state))
    return _pill(tone, escape(words), escape(state))


def _held_for(slot: Mapping[str, Any], now: float) -> str:
    """How long its holder has had it, from the claim."""
    claimed = slot.get("claimed_at")
    if slot["state"] not in slotstates.HELD or not claimed:
        return ""
    age = escape(_age(now, claimed))
    return f'<span class="lbl">held </span><span title="claimed {age} ago">{age}</span>'


def _claude_cell(slot: Mapping[str, Any], node: Mapping[str, Any], report: Mapping[str, Any],
                 said: Optional[Mapping[str, Any]]) -> str:
    """The Claude Code it runs and what that follows, then where it is going."""
    version = (report.get("claude") or {}).get("version") if isinstance(report, Mapping) else None
    if not version:
        return ""
    follows = _follows(claude_versions.slot_target(slot, node))
    return (f'<span class="lbl">Claude Code </span>{escape(str(version))}'
            + (f" ({escape(follows)})" if follows else "")
            + _update_word(slot, node, str(version), said))


def _update_said(slot: Mapping[str, Any], node: Mapping[str, Any], report: Mapping[str, Any],
                 update: Optional[Mapping[str, Any]],
                 channels: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """What the holder's own page says of this slot's Claude Code: only for a
    slot somebody uses. A free one's next holder gets whatever is current."""
    if slot["state"] not in IN_USE or not isinstance(report, Mapping):
        return None
    restart = (report.get("upgrade") or {}).get("restart") == "waiting"
    return claude_versions.status((report.get("claude") or {}).get("version"),
                                  claude_versions.slot_target(slot, node), channels, update,
                                  restart)


def _update_word(slot: Mapping[str, Any], node: Mapping[str, Any], version: str,
                 said: Optional[Mapping[str, Any]]) -> str:
    """At most one word on where a used slot's Claude Code is going. A failed
    update is trouble, and said with the rest of it."""
    if slot["state"] not in IN_USE:
        return ""
    pin = str(node.get("pinned_version") or "")
    # The operator's exact pin on a machine: only it can be behind, since a
    # channel has no number to compare, and nobody else moves it.
    if slot.get("kind") != slotstates.OWNER_SLOT and pin and not is_channel(pin):
        return ' <span class="pill warn">update pending</span>' if version != pin else ""
    kind = (said or {}).get("status")
    if kind == "updating":
        to = (said or {}).get("to")
        where = f" to {escape(str(to))}" if to else ""
        return f' <span class="pill busy">updating{where}</span>'
    if kind == "updated":
        return (' <span class="pill busy" title="Remote Control switches over once no session '
                'is open">restart pending</span>')
    return ' <span class="cc-new">update available</span>' if kind == "available" else ""


def _follows(target: claude_versions.Target) -> str:
    """What a slot's Claude Code follows, in a word or two."""
    if target.held:
        return f"held at {target.version}"
    if target.channel:
        return target.channel
    return f"pinned {target.version}" if target.version else ""


def _releases(channels: Mapping[str, Any], now: float) -> str:
    """Where Anthropic's release channels stood when last read: what a slot's
    holder is offered to move to, said once above every slot."""
    known = [f"{channel.capitalize()} <b>{escape(number)}</b>" for channel in RELEASE_ORDER
             if (number := claude_versions.channel_version(channels, channel))]
    if not known:
        return ('<p class="muted small releases">Claude Code releases: not read yet. The server '
                "reads them about once an hour.</p>")
    checked = channels.get("checked_at")
    when = f" &middot; checked {escape(_age(now, checked))} ago" if checked else ""
    return (f'<p class="muted small releases">Claude Code releases: '
            f'{" &middot; ".join(known)}{when}</p>')


# -- what is wrong with a slot, and with its machine ------------------------------------

def _problems(slot: Mapping[str, Any], report: Mapping[str, Any],
              alerts: list[Mapping[str, Any]], said: Optional[Mapping[str, Any]],
              now: float, listening: Optional[float] = None) -> tuple[list[str], list[str]]:
    """What is wrong with this slot: a pill for each thing, for the eye, and a
    line for each that has more to say. Each is said once."""
    pills, notes = [], []
    for alert in _alerts_for(slot, alerts):
        tone = "critical" if alert["level"] == "critical" else "warn"
        pills.append(_pill(tone, escape(str(alert["rule"]).split(":", 1)[0].replace("_", " "))))
        notes.append(_note(str(alert["message"])))
    # Its holder moved it to another Claude account: said for the week that
    # holds the next change back, which covers every change there is. Never
    # which account; that is theirs to see.
    switched = slot.get("account_switched_at")
    if slotstates.switch_wait_until(switched, now) is not None:
        notes.append(_note(f"changed Claude account {_age(now, switched)} ago", "muted"))
    # A claiming slot always has claimed_at: the claim writes both at once.
    if (slot["state"] == slotstates.CLAIMING
            and now - max(slot["claimed_at"], listening or 0.0) > STUCK_AFTER_S):
        pills.append(_pill("warn", "stuck"))
        notes.append(_note(f"setting up for {_age(now, slot['claimed_at'])}; given up at "
                           f"{slotstates.CLAIM_TIMEOUT_S // 60} min"))
    # The machine's own word on a failed update first; the server's record of
    # the same failure only when the machine said nothing.
    failed = _upgrade_trouble(report)
    if not failed and (said or {}).get("status") == "failed":
        why = (said or {}).get("detail") or "no reason given"
        failed = [f"Claude Code update failed: {why}"]
    if failed:
        pills.append(_pill("critical", "update failed"))
        notes.extend(_note(line) for line in failed)
    return pills, notes


def _alerts_for(slot: Mapping[str, Any],
                alerts: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The open alerts about this slot and no other: a machine names each by
    the slot's Linux user, and two machines both have a slot01."""
    own = slot.get("kind") == slotstates.OWNER_SLOT
    return [a for a in alerts if a["node_id"] == slot["node_id"]
            and (str(a["rule"]).endswith(f":{slot['unix_user']}")
                 or (own and a["rule"] in OWN_NODE_RULES))]


def _upgrade_trouble(report: Mapping[str, Any]) -> list[str]:
    """A Claude Code update the machine tried for this slot and could not make."""
    upgrade = (report.get("upgrade") if isinstance(report, Mapping) else None) or {}
    if upgrade.get("ok") is not False:
        return []
    return [f"Claude Code update to {upgrade.get('to') or '?'} failed: "
            f"{upgrade.get('error') or 'no reason given'}"]


def _machine_problems(node: Mapping[str, Any], rows: tuple[Mapping[str, Any], ...],
                      said: Mapping[str, Any], *, shared: bool) -> tuple[list[str], list[str]]:
    """What is wrong with the machine itself, said on its every row. An
    owner's own node names itself, so only a reboot is worth saying there."""
    pills, notes = [], []
    if said.get("reboot_required") is True:
        pills.append(_pill("warn", "reboot needed"))
    if not shared:
        return pills, notes
    if len(rows) > slotstates.MAX_SLOTS_PER_MACHINE:
        # It answers to its own id, never one holder's name, until the extra
        # slots are taken off.
        pills.append(_pill("warn", "more than one slot",
                           f"claude.ai shows every holder here as {escape(node['id'])}"))
    # It takes its slot's name on its next run; until then claude.ai/code
    # shows the old one. Nothing reported is a machine not yet heard from.
    reported = said.get("hostname")
    wanted = machine_hostname(node["id"], list(rows))
    if reported and reported != wanted:
        pills.append(_pill("warn", "hostname pending"))
        notes.append(_note(f"still answers to {reported}; becomes {wanted} on its next run",
                           "muted"))
    return pills, notes


def _pill(tone: str, words: str, title: str = "") -> str:
    """A pill. The words and the title come escaped."""
    hint = f' title="{title}"' if title else ""
    return f'<span class="pill {tone}"{hint}>{words}</span>'


def _note(text: str, tone: str = "bad-text") -> str:
    """One line under a row. Whatever a machine or a person wrote is text."""
    return f'<li class="{tone}">{escape(text)}</li>'


# -- what the operator can do to a row, under its Manage --------------------------------

def _part(title: str, body: str, hint: str = "") -> str:
    """One part of a row's Manage: a head, its forms, and a line saying what
    they do. The hint comes escaped."""
    said = f'<p class="mhint">{hint}</p>' if hint else ""
    return f'<div class="mpart"><p class="mhead">{escape(title)}</p>{body}{said}</div>'


def _slot_actions(slot: Mapping[str, Any], csrf: str) -> str:
    """Take back a slot somebody holds; forget a free one."""
    base = f"/actions/slot/{escape(slot['id'])}"
    # The name the row shows, which is its holder's while they hold it.
    shown = names.display(slot)
    typed = escape(shown)
    if slot["state"] in slotstates.RELEASABLE:
        # Typed, not pre-filled: this deletes somebody's work.
        box = (f'<input type="text" name="confirm" placeholder="type {typed}" '
               f'size="{max(12, len(shown) + 6)}" autocomplete="off" required '
               f'aria-label="Type {typed} to confirm">')
        return _part("Take back", _form(f"{base}/reclaim", csrf, "Take back", box, "danger"),
                     f"Wipes it: everything on it goes, its Claude sign-in with it. Type "
                     f"<b>{typed}</b> to confirm.")
    if slot["state"] == slotstates.FREE:
        return _part("Remove the slot", _form(f"{base}/remove", csrf, "Remove"),
                     f"Takes {typed} off the machine. It is free, so there is nothing on it "
                     "to lose.")
    return _part("Take back", "", "Being wiped already: it is free again once the machine "
                                  "confirms.")


def _keep_part(node: Mapping[str, Any], csrf: str) -> str:
    """Keep the machine for one account, or open it to anybody again."""
    base = f"/actions/machine/{escape(node['id'])}"
    if node.get("reserved_for"):
        return _part("Reservation", _form(f"{base}/unreserve", csrf, "Clear reservation"),
                     "Once cleared, anybody with an allowance may claim it.")
    return _part("Keep for somebody",
                 _form(f"{base}/reserve", csrf, "Reserve",
                       '<input type="email" name="email" placeholder="their email" size="20" '
                       'aria-label="Email of the account to keep it for" required>'),
                 "While it is free, only they can claim it.")


def _advanced_part(machine: _Machine, csrf: str) -> str:
    """Capacity and declaring the slot: only where either can do anything, a
    machine with no slot yet or one with too many."""
    node = machine.node
    base = f"/actions/machine/{escape(node['id'])}"
    capacity = int(node["capacity"])
    sized = _form(f"{base}/capacity", csrf, "Set capacity",
                  '<input type="text" name="count" class="count" inputmode="numeric" '
                  f'value="{capacity}" aria-label="Capacity" required>')
    if machine.rows:
        return _part("Advanced", sized,
                     "From before one slot per machine: claude.ai shows every holder here as "
                     f"{escape(node['id'])}. Take the extra slot back, then remove it.")
    # The server refuses a second slot, so declaring is only for a machine
    # with none.
    declare = _form(f"{base}/slot-add", csrf, "Declare its slot",
                    '<input type="text" name="slot_id" placeholder="slot id" size="10" '
                    'aria-label="Slot id" required><input type="text" name="unix_user" '
                    'placeholder="unix user" size="10" aria-label="Unix user" required>')
    first = "Set its capacity to 1, then declare its slot" if capacity < 1 else "Its slot"
    return _part("Advanced", declare + sized,
                 f"{first}: usually the machine's own id, {escape(node['id'])}, with the "
                 "unix user slot01.")


def _kept_for(node: Mapping[str, Any], accounts: Mapping[str, Mapping[str, Any]]) -> str:
    """Who this machine's free slot is kept for, when it is anybody; escaped."""
    account_id = node.get("reserved_for")
    if not account_id:
        return ""
    keeper = accounts.get(account_id)
    who = escape(str(keeper["email"])) if keeper else "an account that no longer exists"
    return f"kept for {who}"


def _accounts_card(store: Store, accounts: Mapping[str, Mapping[str, Any]], csrf: str,
                   now: float, plans_held: Mapping[Optional[str], list[str]]) -> str:
    ledger: dict[str, list[dict[str, Any]]] = {}
    for row in store.list_payments():
        ledger.setdefault(row["account_id"], []).append(row)
    lines = []
    for account in sorted(accounts.values(), key=lambda a: str(a["email"])):
        held = store.held_slot_count(account["id"])
        quota = int(account.get("slot_quota") or 0)
        paid = ledger.get(account["id"], [])
        role = " · operator" if account.get("role") == "admin" else ""
        seen = (f"last here {escape(_age(now, account['last_seen_at']))} ago"
                if account.get("last_seen_at") else "never back")
        lines.append(
            f'<div class="row-line"><div class="row-name">{escape(str(account["email"]))}'
            f'<span class="muted">{role} · holds {held}{_plans_said(plans_held, account)}'
            f' · {seen}'
            f"{_standing(paid, bool(held or quota), now)}</span></div>"
            '<div class="actions">'
            + _form(f"/actions/account/{escape(account['id'])}/allowance", csrf,
                    "Set allowance",
                    '<input type="text" name="count" class="count" inputmode="numeric" '
                    f'value="{quota}" required>')
            + "</div>" + _ledger(account, paid, csrf, now) + "</div>")
    body = "".join(lines) or '<p class="quiet">Nobody has signed in yet.</p>'
    return ('<h2 id="accounts">Accounts</h2><div class="card">' + body +
            '<p class="note">An allowance is how many slots somebody may hold; it starts at '
            "zero. Lowering it takes nothing away: they keep what they hold, and only claiming "
            "more stops. Payments are a record for you and nothing more: a lapsed one takes no "
            "slot back and stops no claim. Operators are made on the server: "
            "<code>ccfleetd account role &lt;email&gt; admin</code>.</p></div>")


def _plans_said(plans_held: Mapping[Optional[str], list[str]],
                account: Mapping[str, Any]) -> str:
    """Their slots' Claude plans, beside how many they hold; escaped."""
    said = plans_held.get(account["id"]) or []
    return f" ({escape(', '.join(said))})" if said else ""


def _standing(paid: list[Mapping[str, Any]], counts: bool, now: float) -> str:
    """Paid up, lapsed, or nothing written down, in the words the operator scans for.

    Lapsed is only called out while it still matters: somebody who holds slots
    or may claim them. Somebody who has left has an ended payment, not a debt.
    """
    through = payments.paid_through(paid)
    state = payments.standing(through, now)
    if state == payments.NONE:
        return " · no payments"
    # A date split at its hyphens on a phone reads as two numbers.
    day = f'<span class="nowrap">{escape(str(through))}</span>'
    if state == payments.PAID:
        return f" · paid through {day}"
    if counts:
        return f' · <span class="bad-text">lapsed: paid through {day}</span>'
    return f" · paid through {day}, ended"


def _ledger(account: Mapping[str, Any], paid: list[Mapping[str, Any]], csrf: str,
            now: float) -> str:
    items = "".join(_payment_line(p, csrf) for p in paid)
    # Whatever they paid in last time is the likeliest this time.
    currency = paid[0]["currency"] if paid else "USD"
    # And a month, from the end of what they have paid for or from today.
    through = payments.next_through(payments.paid_through(paid), payments.today(now))
    record = _form(
        f"/actions/account/{escape(account['id'])}/payment", csrf, "Record payment",
        '<input type="text" name="amount" placeholder="amount" inputmode="decimal" '
        'size="7" required>'
        f'<input type="text" name="currency" value="{escape(currency)}" size="4" '
        'maxlength="3" required>'
        f'<input type="date" name="through" title="paid through" value="{escape(through)}" '
        'required>'
        '<input type="text" name="note" placeholder="note, seen only here" size="18" '
        f'maxlength="{payments.NOTE_MAX}">')
    return (f'<details class="ledger"><summary>Payments ({len(paid)})</summary>'
            f"{items}{record}</details>")


def _payment_line(row: Mapping[str, Any], csrf: str) -> str:
    text = (f"{payments.today(row['recorded_at']).isoformat()} · "
            f"{escape(payments.format_amount(row['amount_minor'], row['currency']))} · "
            f"through {escape(row['paid_through'])}"
            + (f" · {escape(row['note'])}" if row["note"] else "")
            + f" · by {escape(row['recorded_by'])}")
    if row["voided_at"] is not None:
        return (f'<div class="payment voided"><s>{text}</s> '
                '<span class="pill disabled">voided</span></div>')
    return (f'<div class="payment">{text} '
            + _form(f"/actions/payment/{int(row['id'])}/void", csrf, "Void", cls="danger")
            + "</div>")


def _count(form: Mapping[str, str]) -> int:
    raw = form.get("count") or ""
    if not COUNT_RE.fullmatch(raw):
        raise StoreError("that needs a whole number from 0 to 9999")
    return int(raw)


def act(store: Store, kind: str, target: str, action: str, form: Mapping[str, str],
        now: float, *, by: str) -> Optional[str]:
    """One of the operator's slot actions. Returns where to send them back to,
    or None when there is no such action. Raises StoreError to refuse.

    `by` is who is acting, as the console knows them; the ledger writes it
    beside every payment.
    """
    if kind == "machine" and action == "capacity":
        count = _count(form)
        if count > slotstates.MAX_SLOTS_PER_MACHINE:
            raise StoreError(f"{slotstates.ONE_SLOT_WHY}; a machine's capacity is 0 or 1")
        if not store.set_machine_capacity(target, count):
            raise StoreError(f"no machine {target!r}")
        return "slots"
    if kind == "machine" and action == "slot-add":
        already = store.list_slots(node_id=target, kind=slotstates.MACHINE_SLOT)
        if len(already) >= slotstates.MAX_SLOTS_PER_MACHINE:
            raise StoreError(f"{target} already has its slot ({already[0]['id']}): "
                             f"{slotstates.ONE_SLOT_WHY}")
        store.add_slot(form.get("slot_id", "").strip(), target,
                       form.get("unix_user", "").strip(), now=now)
        return "slots"
    if kind == "machine" and action == "reserve":
        # By address, the way the operator knows people; an address nobody has
        # signed in with is refused, never stored as a promise to nobody.
        email = (form.get("email") or "").strip()
        if not email:
            raise StoreError("type the email address of the account to keep this machine for")
        keeper = store.account_by_email(email)
        if keeper is None:
            raise StoreError(f"nobody has signed in as {email}; they sign in once, then "
                             "the machine can be kept for them")
        store.reserve_machine(target, keeper["id"])
        return "slots"
    if kind == "machine" and action == "unreserve":
        store.reserve_machine(target, None)
        return "slots"
    if kind == "slot" and action == "reclaim":
        try:
            # The operator's side: any holder. It is a release like any other,
            # with the same wipe, and the sign-in in flight goes with it. By the
            # name the row showed, checked as the release starts: a slot given
            # back and claimed by somebody else since the page was drawn has
            # another name by then, and the old one no longer matches.
            store.begin_release(target, named=form.get("confirm") or "")
        except slotstates.TransitionError as exc:     # a stale form: already on its way out
            raise StoreError(str(exc)) from exc
        return "slots"
    if kind == "slot" and action == "quota":
        # The operator's Refresh on the usage card: any holder's slot in use.
        store.request_quota_read(target, now)
        return "usage"
    if kind == "slot" and action == "remove":
        store.remove_slot(target)       # refuses anything but a free slot: nothing to lose
        return "slots"
    if kind == "account" and action == "allowance":
        if not store.set_slot_quota(target, _count(form)):
            raise StoreError("no such account")
        return "accounts"
    if kind == "account" and action == "payment":
        store.record_payment(target, amount=form.get("amount", ""),
                             currency=form.get("currency", ""),
                             through=form.get("through", ""), note=form.get("note", ""),
                             recorded_by=by, now=now)
        return "accounts"
    if kind == "payment" and action == "void":
        if not PAYMENT_ID_RE.fullmatch(target):
            raise StoreError("no such payment")
        store.void_payment(int(target), now=now)
        return "accounts"
    if kind == "price" and target == PRICE_TARGET and action == "set":
        store.set_price(form.get("amount", ""), form.get("currency", ""), by=by, now=now)
        return "price"
    if kind == "price" and target == PRICE_TARGET and action == "clear":
        store.clear_price()
        return "price"
    return None
