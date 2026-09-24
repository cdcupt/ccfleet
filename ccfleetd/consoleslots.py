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
from html import escape
from typing import Any, Optional

from . import claude_versions, names, payments, pricing
from . import slots as slotstates
from .desired import is_channel, machine_hostname
from .render import _age
from .store import Store, StoreError

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

# The same meanings as everywhere else: green running, amber waiting on its
# holder, the accent while the machine works on it, grey empty.
STATE_TONE = {slotstates.FREE: "disabled", slotstates.CLAIMING: "busy",
              slotstates.CLAIMED: "warn", slotstates.ACTIVE: "ok",
              slotstates.RELEASING: "busy"}


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


def _upgrade_trouble(report: Mapping[str, Any]) -> list[str]:
    """A Claude Code update the machine tried for this slot and could not make."""
    upgrade = report.get("upgrade") or {}
    if upgrade.get("ok") is not False:
        return []
    return [f"Claude Code update to {upgrade.get('to') or '?'} failed: "
            f"{upgrade.get('error') or 'no reason given'}"]


def section(store: Store, csrf: str, now: float) -> str:
    """The slots card, the accounts card and the price, for an admin's console."""
    accounts = {a["id"]: a for a in store.list_accounts()}
    return (_slots_card(store, accounts, csrf, now) + _accounts_card(store, accounts, csrf, now)
            + _price_card(store, csrf, now))


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


def _slots_card(store: Store, accounts: Mapping[str, Mapping[str, Any]], csrf: str,
                now: float) -> str:
    alerts = store.open_alerts()
    latest = store.latest_heartbeats()
    blocks = []
    for node in store.list_nodes():
        all_rows = store.list_slots(node_id=node["id"])
        rows = [r for r in all_rows if r["kind"] == slotstates.MACHINE_SLOT]
        capacity = int(node["capacity"])
        # What the machine last said: each slot's own report, and its own state.
        said = (latest.get(node["id"]) or {}).get("payload") or {}
        reboot = (' <span class="pill warn">reboot needed</span>'
                  if said.get("reboot_required") is True else "")
        owned = [r for r in all_rows if r["kind"] == slotstates.OWNER_SLOT]
        if owned and not rows:
            blocks.append(_own_machine_line(owned[0], accounts, reboot,
                                            _running(said, owned[0], node)))
            continue
        # A machine is one with its slot declared, or one whose agent says it
        # is one — with one slot per machine, capacity no longer tells.
        if not rows and capacity <= 1 and said.get("mode") != slotstates.MACHINE_MODE:
            continue          # an ordinary owner node: nothing about slots to show
        reports = {r.get("unix_user"): r for r in said.get("slots") or []
                   if isinstance(r, Mapping)}
        base = f"/actions/machine/{escape(node['id'])}"
        # From before one slot per machine: it answers to its own id, never to
        # one holder's name, until the extra slots are taken off.
        crowded = (' <span class="pill warn">more than one slot: claude.ai shows every '
                   f"holder here as {escape(node['id'])}; take the extra off</span>"
                   if len(rows) > slotstates.MAX_SLOTS_PER_MACHINE else "")
        head = (f'<div class="row-line"><div class="row-name">{escape(node["id"])}'
                f'<span class="muted"> · {len(rows)} of {capacity} declared</span>{reboot}'
                f"{crowded}{_hostname_pending(node['id'], rows, said)}"
                f"{_kept_for(node, accounts)}</div>"
                '<div class="actions">'
                + _form(f"{base}/capacity", csrf, "Set capacity",
                        '<input type="text" name="count" class="count" inputmode="numeric" '
                        f'value="{capacity}" required>')
                + _form(f"{base}/slot-add", csrf, "Declare a slot",
                        '<input type="text" name="slot_id" placeholder="slot id" size="10" '
                        'required><input type="text" name="unix_user" placeholder="unix user" '
                        'size="10" required>')
                + _form(f"{base}/reserve", csrf, "Reserve",
                        '<input type="email" name="email" placeholder="keep for (email)" '
                        'size="18" required>')
                + (_form(f"{base}/unreserve", csrf, "Clear reservation")
                   if node.get("reserved_for") else "")
                + "</div></div>")
        pin = str(node.get("pinned_version") or "")
        lines = [head] + [_slot_line(s, accounts, alerts, csrf, now,
                                     reports.get(s["unix_user"]) or {}, pin,
                                     _follows(claude_versions.slot_target(s, node)))
                          for s in rows]
        blocks.append("".join(lines))
    body = "".join(blocks) or ('<p class="quiet">No shared machines yet. A machine joins '
                               "with its one slot, declared on the server: "
                               "<code>ccfleetd slot add &lt;machine&gt; --machine "
                               "&lt;machine&gt; --unix-user slot01</code>.</p>")
    return ('<h2 id="slots">Slots</h2><div class="card">'
            + _releases(store.get_channel_versions(), now) + body +
            '<p class="note">Taking a slot back is a release: the machine wipes it, and it is '
            "free again once the machine confirms the Linux user is gone. There is no way here "
            "to sign in as anybody or to finish anybody's Claude sign-in, by design.</p></div>")


def _releases(channels: Mapping[str, Any], now: float) -> str:
    """Where Anthropic's release channels stood when last read: what a slot's
    holder is offered to move to, said once above every slot."""
    known = [f"{channel} <b>{escape(number)}</b>" for channel in claude_versions.CHANNELS
             if (number := claude_versions.channel_version(channels, channel))]
    if not known:
        return ('<p class="muted small">Claude Code releases: not read yet. The server reads '
                "them about once an hour.</p>")
    checked = channels.get("checked_at")
    when = f" &middot; checked {escape(_age(now, checked))} ago" if checked else ""
    return (f'<p class="muted small">Claude Code releases: {" &middot; ".join(known)}'
            f"{when}</p>")


def _follows(target: claude_versions.Target) -> str:
    """What a slot's Claude Code follows, in a word or two."""
    if target.held:
        return f"held at {target.version}"
    if target.channel:
        return target.channel
    return f"pinned {target.version}" if target.version else ""


def _running(said: Mapping[str, Any], slot: Mapping[str, Any],
             node: Mapping[str, Any]) -> str:
    """The Claude Code an owner's own node says it runs, and what it follows."""
    version = (said.get("claude") or {}).get("version") if isinstance(said, Mapping) else None
    follows = _follows(claude_versions.slot_target(slot, node))
    if not version:
        return ""
    return f" · Claude Code {escape(str(version))}" + (f" ({escape(follows)})" if follows else "")


def _hostname_pending(node_id: str, rows: list[Mapping[str, Any]],
                      said: Mapping[str, Any]) -> str:
    """The name the machine still answers to, when it is not its slot's yet.

    It takes the new one on its next run; until then claude.ai/code shows the
    old one, which is worth the operator knowing. Nothing reported, nothing
    said: that is a machine that has not checked in, not one that is behind.
    """
    reported = said.get("hostname")
    wanted = machine_hostname(node_id, list(rows))
    if not reported or reported == wanted:
        return ""
    return (f' <span class="pill warn">hostname pending: answers to {escape(str(reported))}, '
            f"becoming {escape(wanted)}</span>")


def _own_machine_line(slot: Mapping[str, Any], accounts: Mapping[str, Mapping[str, Any]],
                      reboot: str, running: str = "") -> str:
    """Somebody's own node, counted as their slot. Nothing here acts on it:
    ccfleet never wipes, hands out or provisions anything on an owner's node."""
    holder = accounts.get(slot.get("held_by") or "")
    who = escape(str(holder["email"])) if holder else "&mdash;"
    return (f'<div class="row-line"><div class="row-name">{escape(names.display(slot))}'
            f'<span class="muted"> · own machine · {escape(slot["unix_user"])}{running}</span>'
            f'{reboot} <span class="pill ok">{escape(slot["state"])}</span>'
            f' <span class="small">{who}</span></div>'
            '<div class="actions"><span class="muted small">counted as their slot; '
            "let go with <code>ccfleetd node hold "
            f'{escape(slot["node_id"])} --none</code></span></div></div>')


def _kept_for(node: Mapping[str, Any], accounts: Mapping[str, Mapping[str, Any]]) -> str:
    """Who this machine's free slots are kept for, when it is anybody."""
    account_id = node.get("reserved_for")
    if not account_id:
        return ""
    keeper = accounts.get(account_id)
    who = escape(str(keeper["email"])) if keeper else "an account that no longer exists"
    return f' <span class="pill ok">Reserved for {who}</span>'


def _slot_line(slot: Mapping[str, Any], accounts: Mapping[str, Mapping[str, Any]],
               alerts: list[Mapping[str, Any]], csrf: str, now: float,
               report: Optional[Mapping[str, Any]] = None, pin: str = "",
               follows: str = "") -> str:
    report = report or {}
    holder = accounts.get(slot.get("held_by") or "")
    who = escape(str(holder["email"])) if holder else "&mdash;"
    seen = {1: "on machine", 0: "not on machine"}.get(slot.get("present"), "not yet seen")
    claimed = (f" · claimed {escape(_age(now, slot['claimed_at']))} ago"
               if slot.get("claimed_at") else "")
    version = (report.get("claude") or {}).get("version")
    running = (f" · Claude Code {escape(str(version))}"
               + (f" ({escape(follows)})" if follows else "")) if version else ""
    # Only an exact pin can be behind; a channel has no number to compare. And
    # only for a slot somebody holds: a free one has no Claude Code to update.
    pending = (' <span class="pill warn">update pending</span>'
               if version and pin and not is_channel(pin) and version != pin
               and slot["state"] in (slotstates.CLAIMED, slotstates.ACTIVE) else "")
    trouble = "".join(f'<br><span class="bad-text">{escape(t)}</span>'
                      for t in _trouble(slot, alerts, now) + _upgrade_trouble(report))
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
    # The name its holder and claude.ai know it by; the id, which every
    # command takes, beside it whenever the two differ.
    also = f" · {escape(slot['id'])}" if names.display(slot) != slot["id"] else ""
    return (f'<div class="row-line"><div class="row-name">{escape(names.display(slot))}'
            f'<span class="muted">{also} · {escape(slot["unix_user"])} · {escape(seen)}{claimed}'
            f"{running}</span> <span class=\"pill {tone}\">{escape(slot['state'])}</span>"
            f"{pending}"
            f" <span class=\"small\">{who}</span>{trouble}</div>"
            f'<div class="actions">{buttons}</div></div>')


def _accounts_card(store: Store, accounts: Mapping[str, Mapping[str, Any]], csrf: str,
                   now: float) -> str:
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
            f'<span class="muted">{role} · holds {held} · {seen}'
            f"{_standing(paid, bool(held or quota), now)}</span></div>"
            '<div class="actions">'
            + _form(f"/actions/account/{escape(account['id'])}/allowance", csrf,
                    "Set allowance",
                    '<input type="text" name="count" class="count" inputmode="numeric" '
                    f'value="{quota}" required>')
            + "</div>" + _ledger(account, paid, csrf) + "</div>")
    body = "".join(lines) or '<p class="quiet">Nobody has signed in yet.</p>'
    return ('<h2 id="accounts">Accounts</h2><div class="card">' + body +
            '<p class="note">An allowance is how many slots somebody may hold; it starts at '
            "zero. Lowering it takes nothing away: they keep what they hold, and only claiming "
            "more stops. Payments are a record for you and nothing more: a lapsed one takes no "
            "slot back and stops no claim. Operators are made on the server: "
            "<code>ccfleetd account role &lt;email&gt; admin</code>.</p></div>")


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


def _ledger(account: Mapping[str, Any], paid: list[Mapping[str, Any]], csrf: str) -> str:
    items = "".join(_payment_line(p, csrf) for p in paid)
    # Whatever they paid in last time is the likeliest this time.
    currency = paid[0]["currency"] if paid else "USD"
    record = _form(
        f"/actions/account/{escape(account['id'])}/payment", csrf, "Record payment",
        '<input type="text" name="amount" placeholder="amount" inputmode="decimal" '
        'size="7" required>'
        f'<input type="text" name="currency" value="{escape(currency)}" size="4" '
        'maxlength="3" required>'
        '<input type="date" name="through" title="paid through" required>'
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
