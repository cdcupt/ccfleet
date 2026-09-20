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
        return None
    block = {"requested_at": login.get("requested_at"),
             "email": login.get("email") or ""}
    code = login.get("code") or ""
    if code and login.get("state") == "code_sent":
        block["code"] = code
    return block


def desired_state(node: Mapping[str, Any],
                  login: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """What this node should look like, derived from its stored row."""
    pending = _login_block(login)
    return {
        "claude_version": _version_target(node.get("pinned_version")),
        "remote_control": bool(node.get("rc_expected")),
        "login": pending,
        "poll_s": LOGIN_POLL_S if pending else IDLE_POLL_S,
    }
