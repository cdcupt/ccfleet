"""The desired state a node is handed on every heartbeat.

The server never opens a connection to a node: nodes are single-owner machines on
residential addresses behind NAT, and a management plane that dials into them is
the shape this project exists not to be. So intent travels the only way it can —
down the response to a request the node itself made.

It is declarative rather than a command queue. Each heartbeat carries the whole
desired state, the agent compares it against reality and acts on the difference,
and the outcome comes back in the next heartbeat. A missed beat costs nothing:
the next one carries the same intent, and applying it twice is a no-op.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from typing import Any, Optional

from . import claude_versions, names

# A node may be pinned to an exact version, or told to track a channel. Anything
# else is refused rather than passed to the installer: this string reaches
# `claude install <target>` on the node.
VERSION_CHANNELS = ("stable", "latest")
MAX_VERSION_LEN = 40

# What a node may be asked to run. "login" signs the node itself in; "token"
# mints a one-year device credential the owner takes to their own machine;
# "switch", only ever for a machine's slot, signs it in to another Claude
# account of its holder's. Anything else is coerced to "login" rather than
# forwarded: this word decides which command the agent runs. An agent from
# before "switch" coerces it the same way, and a slot that keeps its account
# then refuses the other one: nothing changes on it.
LOGIN_KINDS = ("login", "token", "switch")


# The verification URL is supplied by a node and then shown to an operator as a
# link. Escaping makes it safe as *text*; it does nothing about the scheme, and
# href="javascript:..." survives escaping intact. So the URL is checked, not
# merely escaped, and a node that offers anything else gets no link at all.
# Measured against a live sign-in rather than guessed: the URL Claude Code
# actually prints is on claude.com, not claude.ai. The .ai hosts stay because
# older builds used them and an operator may still be handed one.
LOGIN_URL_HOSTS = ("claude.com", "www.claude.com", "platform.claude.com",
                   "claude.ai", "www.claude.ai", "console.anthropic.com")


def is_login_url(url: Any) -> bool:
    """True only for an https URL on a host we expect a sign-in to live on."""
    if not isinstance(url, str) or len(url) > 1024:
        return False
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    # netloc rather than hostname so an embedded port or credential cannot hide.
    return parsed.hostname is not None and parsed.hostname.lower() in LOGIN_URL_HOSTS


def is_channel(pin: Any) -> bool:
    """True when a pin names a channel rather than an exact version.

    A channel has no number to compare an installed version against, so every
    caller that asks "does the node match its pin?" has to ask this first.
    """
    return isinstance(pin, str) and pin.strip() in VERSION_CHANNELS


def _version_target(raw: Any) -> str:
    """The version a node should be running, or "" meaning leave it alone.

    An unusable pin is dropped rather than forwarded. The node would refuse it
    anyway, but a pin that cannot work should not reach the node at all.
    """
    if not isinstance(raw, str):
        return ""
    target = raw.strip()
    if not target or len(target) > MAX_VERSION_LEN:
        return ""
    if target in VERSION_CHANNELS:
        return target
    # An exact version: digits and dots, plus an optional pre-release suffix.
    if all(ch.isalnum() or ch in ".-+" for ch in target) and target[0].isdigit():
        return target
    return ""


# A node normally reports every few minutes. That is far too slow for a sign-in,
# where someone is watching the console waiting for a URL, so the agent is told
# to come back quickly while one is in flight.
IDLE_POLL_S = 300
LOGIN_POLL_S = 5


def _login_block(login: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """The sign-in a node should be working on, or None.

    `requested_at` is what lets the agent tell a new request from one it has
    already acted on, so a repeated heartbeat does not restart a login in
    progress. The code is only present once someone has pasted one.
    """
    if not login or login.get("state") not in ("requested", "url_ready", "code_sent"):
        # 'ready' is deliberately absent: a minted token is waiting for a person,
        # not for the node. Sending the block again would restart the flow.
        return None
    kind = login.get("kind")
    block = {"requested_at": login.get("requested_at"),
             "email": login.get("email") or "",
             "kind": kind if kind in LOGIN_KINDS else "login"}
    code = login.get("code") or ""
    if code and login.get("state") == "code_sent":
        block["code"] = code
    return block


# The slot states a sign-in can run in: set up and held. Anything else is
# either not provisioned yet or on its way to being wiped.
SLOT_SIGN_IN_STATES = ("claimed", "active")


def _update_block(update: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """An update somebody asked for from their page, while it waits: the agent
    installs now instead of at its next check. Named by its own time, so an
    answer can only ever close the request it answers."""
    if not update or update.get("state") != "pending":
        return None
    requested_at = update.get("requested_at")
    if not isinstance(requested_at, (int, float)) or isinstance(requested_at, bool):
        return None
    return {"requested_at": requested_at}


def _version_fields(target: claude_versions.Target, channels: Mapping[str, Any],
                    update: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """What to install, the number that channel stands at, and whether now.

    The number lets an agent see a new release within the hour instead of at
    its own daily check. The operator's hold carries neither an update nor a
    number: nothing on a page moves a held machine. Nothing to aim for says
    nothing at all, so a slot with no pin keeps the shape it always had.
    """
    version = _version_target(target.version)
    if not version:
        return {}
    fields: dict[str, Any] = {"claude_version": version}
    number = claude_versions.channel_version(channels, target.channel)
    if number:
        fields["channel_version"] = number
    pending = None if target.held else _update_block(update)
    if pending:
        fields["update_now"] = pending
    return fields


def _slot_block(slot: Mapping[str, Any],
                login: Optional[Mapping[str, Any]] = None,
                node: Optional[Mapping[str, Any]] = None,
                channels: Optional[Mapping[str, Any]] = None,
                update: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """What a shared machine should do about one of its slots.

    The state is the whole instruction: `claiming` means provision it,
    `releasing` means wipe it, and everything else means leave it and report.
    A claim carries its own timestamp, which is how the machine says which
    claim it finished — so news about an earlier claim of the same slot can
    never complete a later one. A sign-in its holder has started rides along,
    in exactly the shape a node's own does.
    """
    block: dict[str, Any] = {"unix_user": slot.get("unix_user"),
                             "state": slot.get("state")}
    if slot.get("state") == "claiming":
        block["claimed_at"] = slot.get("claimed_at")
    pending = _login_block(login) if slot.get("state") in SLOT_SIGN_IN_STATES else None
    if pending:
        block["login"] = pending
    # Its own Claude Code, only while somebody holds it set up: a slot being
    # made or wiped has nothing to update. An agent from before this reads the
    # machine's pin above and ignores these, which is the old behaviour.
    if node is not None and slot.get("state") in SLOT_SIGN_IN_STATES:
        block.update(_version_fields(claude_versions.slot_target(slot, node),
                                     channels or {}, update))
    return block


def machine_hostname(node_id: str, slots: list[Mapping[str, Any]]) -> str:
    """What a shared machine should call itself: its one slot's name.

    One machine is one slot, and claude.ai/code shows a machine by its
    hostname, so the machine answers to whatever the slot is called — its
    holder's name while held, its id while free. With no slot, or a slot id
    that is no hostname, the machine keeps its own id, which always is one.
    """
    # Exactly one: a machine from before one slot per machine may still carry
    # several, and giving it one holder's name would show the others under it.
    if len(slots) == 1:
        name = names.display(slots[0])
        if names.valid_hostname(name):
            return name
    return node_id


def desired_state(node: Mapping[str, Any],
                  login: Optional[Mapping[str, Any]] = None,
                  slots: Optional[list[Mapping[str, Any]]] = None,
                  slot_logins: Optional[Mapping[str, Mapping[str, Any]]] = None,
                  hostname: Optional[str] = None,
                  channels: Optional[Mapping[str, Any]] = None,
                  slot_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
                  own_update: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """What this node should look like, derived from its stored row.

    `slot_logins` maps a slot's id to its sign-in row, for a shared machine.
    `hostname` is what a shared machine should answer to; an owner's node is
    never told one. `channels` is the last read of Anthropic's release
    channels; `slot_updates` maps a slot's id to the update its holder asked
    for, and `own_update` is the one asked for on an owner's own node.
    """
    pending = _login_block(login)
    desired: dict[str, Any] = {
        "claude_version": _version_target(node.get("pinned_version")),
        "remote_control": bool(node.get("rc_expected")),
        "login": pending,
    }
    if hostname is not None:
        desired["hostname"] = hostname
    # Only a machine with slots declared on it hears about slots at all; an
    # ordinary node's reply stays exactly what it was.
    if slots:
        logins = slot_logins or {}
        updates = slot_updates or {}
        desired["slots"] = [_slot_block(s, logins.get(s.get("id")), node, channels or {},
                                        updates.get(s.get("id"))) for s in slots]
    elif hostname is None:
        # An owner's own node: its pin is its owner's, so it is never a hold.
        own = claude_versions.slot_target({"kind": "owner"}, node)
        fields = _version_fields(own, channels or {}, own_update)
        fields.pop("claude_version", None)          # already said, unchanged
        desired.update(fields)
    # Somebody is watching a page, for a URL or an update, on the node or a slot.
    waiting = (pending or "update_now" in desired
               or any("login" in b or "update_now" in b for b in desired.get("slots", ())))
    desired["poll_s"] = LOGIN_POLL_S if waiting else IDLE_POLL_S
    return desired
