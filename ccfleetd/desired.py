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

from collections.abc import Mapping
from typing import Any

# A node may be pinned to an exact version, or told to track a channel. Anything
# else is refused rather than passed to the installer: this string reaches
# `claude install <target>` on the node.
VERSION_CHANNELS = ("stable", "latest")
MAX_VERSION_LEN = 40


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


def desired_state(node: Mapping[str, Any]) -> dict[str, Any]:
    """What this node should look like, derived from its stored row."""
    return {
        "claude_version": _version_target(node.get("pinned_version")),
        "remote_control": bool(node.get("rc_expected")),
    }
