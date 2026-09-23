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

# A node may be pinned to an exact version, or told to track a channel. Anything
# else is refused rather than passed to the installer: this string reaches
# `claude install <target>` on the node.
VERSION_CHANNELS = ("stable", "latest")
MAX_VERSION_LEN = 40

# What a node may be asked to run. "login" signs the node itself in; "token"
# mints a one-year device credential the owner takes to their own machine.
# Anything else is coerced to "login" rather than forwarded: this word decides
# which command the agent runs.
LOGIN_KINDS = ("login", "token")

# The Claude accounts a slot can keep signed in, named by their place on the
# slot. The machine keeps the sign-ins; these names are all that crosses the
# wire about which one is meant, and nothing about them identifies anybody.
SLOT_ACCOUNT_IDS = ("1", "2", "3")
# Where a sign-in on a slot lands: a new account, or one already there.
SIGN_IN_TARGETS = ("new",) + SLOT_ACCOUNT_IDS
# What a slot's holder can ask about an account already on it.
ACCOUNT_ACTIONS = ("use", "forget")


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
    # Which of a slot's accounts the sign-in is for. Absent means the active
    # one, which is what every sign-in meant before a slot could hold more:
    # a machine that predates accounts gets exactly the block it always did.
    if login.get("account") in SIGN_IN_TARGETS:
        block["account"] = login["account"]
    return block


def _account_block(intent: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """What the holder asked about an account on the slot, while it waits.

    A request that failed has been answered; asking the machine again would
    undo the answer the holder is reading. Anything not in the vocabulary is
    dropped rather than forwarded, like a sign-in's kind.
    """
    if (not intent or intent.get("state") != "requested"
            or intent.get("action") not in ACCOUNT_ACTIONS
            or intent.get("account") not in SLOT_ACCOUNT_IDS):
        return None
    return {"action": intent["action"], "id": intent["account"],
            "requested_at": intent.get("requested_at")}


# The slot states a sign-in can run in: set up and held. Anything else is
# either not provisioned yet or on its way to being wiped.
SLOT_SIGN_IN_STATES = ("claimed", "active")


def _slot_block(slot: Mapping[str, Any],
                login: Optional[Mapping[str, Any]] = None,
                intent: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """What a shared machine should do about one of its slots.

    The state is the whole instruction: `claiming` means provision it,
    `releasing` means wipe it, and everything else means leave it and report.
    A claim carries its own timestamp, which is how the machine says which
    claim it finished — so news about an earlier claim of the same slot can
    never complete a later one. A sign-in its holder has started rides along,
    in exactly the shape a node's own does, and so does a request about the
    accounts already on it. Both only for a slot that is set up and held.
    """
    block: dict[str, Any] = {"unix_user": slot.get("unix_user"),
                             "state": slot.get("state")}
    if slot.get("state") == "claiming":
        block["claimed_at"] = slot.get("claimed_at")
    if slot.get("state") not in SLOT_SIGN_IN_STATES:
        return block
    pending = _login_block(login)
    if pending:
        block["login"] = pending
    asked = _account_block(intent)
    if asked:
        block["account"] = asked
    return block


def desired_state(node: Mapping[str, Any],
                  login: Optional[Mapping[str, Any]] = None,
                  slots: Optional[list[Mapping[str, Any]]] = None,
                  slot_logins: Optional[Mapping[str, Mapping[str, Any]]] = None,
                  slot_intents: Optional[Mapping[str, Mapping[str, Any]]] = None
                  ) -> dict[str, Any]:
    """What this node should look like, derived from its stored row.

    `slot_logins` maps a slot's id to its sign-in row, and `slot_intents` to
    what its holder asked about its accounts, for a shared machine.
    """
    pending = _login_block(login)
    desired: dict[str, Any] = {
        "claude_version": _version_target(node.get("pinned_version")),
        "remote_control": bool(node.get("rc_expected")),
        "login": pending,
    }
    # Only a machine with slots declared on it hears about slots at all; an
    # ordinary node's reply stays exactly what it was.
    if slots:
        logins, intents = slot_logins or {}, slot_intents or {}
        desired["slots"] = [_slot_block(s, logins.get(s.get("id")), intents.get(s.get("id")))
                            for s in slots]
    # Somebody is watching a page: for a URL, on the node or on any slot, or
    # for a switch of accounts to land.
    waiting = pending or any("login" in b or "account" in b
                             for b in desired.get("slots", ()))
    desired["poll_s"] = LOGIN_POLL_S if waiting else IDLE_POLL_S
    return desired
