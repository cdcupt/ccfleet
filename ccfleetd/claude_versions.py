"""Which Claude Code a slot runs, which release is newest, and whether to move.

Anthropic publishes the release channels as two tiny files, the same ones its
own installer reads (https://claude.ai/install.sh): ``.../latest`` and
``.../stable``, each a bare version string. The server reads them about once an
hour so a page can say "latest is 2.1.281" and offer to move there. That is the
whole of it: nothing here installs anything; a slot's own agent does, told what
to aim for in the heartbeat reply.

Who decides a slot's version, strongest first:

1. an operator's exact pin on a shared machine, the hold: a safety valve for a
   release that misbehaves, which nobody on the page can override;
2. the channel the slot's holder chose on their page;
3. the machine's own channel, ``stable`` for every machine the SOP makes.

Somebody's own node, counted as their slot, is theirs outright: its pin is
whatever its owner chose, exact or not, and it is never a hold.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import slots as slotstates

log = logging.getLogger("ccfleetd")

CHANNELS = ("stable", "latest")
RELEASES_URL = "https://downloads.claude.ai/claude-code-releases/{channel}"
#: A release as the channel files spell it. Nothing looser is shown or sent.
VERSION_RE = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,6}")
FETCH_EVERY_S = 3600.0
FETCH_TIMEOUT_S = 10.0
#: The files hold a version and a newline; anything longer is not one.
MAX_BODY = 64
#: Where the last good answer is kept, in the settings table.
SETTING_KEY = "claude_channels"

#: A url in, the body's text out. Raises on any failure.
Fetcher = Callable[[str], str]


def parse_version(value: Any) -> Optional[tuple[int, int, int]]:
    """(2, 1, 281) for "2.1.281"; None for anything that is not a release."""
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value.strip()):
        return None
    major, minor, patch = (int(part) for part in value.strip().split("."))
    return major, minor, patch


def is_newer(candidate: Any, than: Any) -> bool:
    """True only when both are releases and `candidate` is the later one: an
    unknown version is never called older, so nothing is offered on a guess."""
    a, b = parse_version(candidate), parse_version(than)
    return a is not None and b is not None and a > b


def default_fetcher(url: str) -> str:  # pragma: no cover - the network
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as resp:  # noqa: S310
        return resp.read(MAX_BODY + 1).decode("ascii", "replace")


def fetch_channel(channel: str, fetcher: Fetcher) -> Optional[str]:
    """The version a channel names right now, or None: a network failure, an
    HTTP error, an oversized body or anything that is not a release."""
    try:
        body = fetcher(RELEASES_URL.format(channel=channel))
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the old value stands
        log.warning("could not read the %s channel: %s", channel, exc)
        return None
    if not isinstance(body, str) or len(body) > MAX_BODY:
        log.warning("the %s channel answered with something that is not a version", channel)
        return None
    text = body.strip()
    if not VERSION_RE.fullmatch(text):
        log.warning("the %s channel answered with something that is not a version", channel)
        return None
    return text


def refresh(known: Mapping[str, Any], now: float,
            fetcher: Fetcher) -> Optional[dict[str, Any]]:
    """A new record when a check is due, else None.

    A channel whose read fails keeps the last good version, with the time it
    was last read, so a page never loses the number to a network blip; the
    check time moves on either way, so a failing source is tried again in an
    hour rather than on every pass.
    """
    checked = known.get("checked_at")
    if _number(checked) and now - float(checked) < FETCH_EVERY_S:
        return None
    record: dict[str, Any] = {"checked_at": now}
    for channel in CHANNELS:
        got = fetch_channel(channel, fetcher)
        if got is not None:
            record[channel] = {"version": got, "fetched_at": now}
            continue
        old = known.get(channel)
        if isinstance(old, Mapping) and parse_version(old.get("version")):
            record[channel] = {"version": str(old["version"]),
                               "fetched_at": old.get("fetched_at")}
    return record


def from_json(raw: Any) -> dict[str, Any]:
    """The stored record, checked again on the way out: a hand edit that is no
    longer a release is dropped rather than shown."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else {}
    except ValueError:
        return {}
    if not isinstance(data, Mapping):
        return {}
    out: dict[str, Any] = {}
    if _number(data.get("checked_at")):
        out["checked_at"] = float(data["checked_at"])
    for channel in CHANNELS:
        entry = data.get(channel)
        if isinstance(entry, Mapping) and parse_version(entry.get("version")):
            fetched = entry.get("fetched_at")
            out[channel] = {"version": str(entry["version"]).strip(),
                            "fetched_at": float(fetched) if _number(fetched) else None}
    return out


def to_json(record: Mapping[str, Any]) -> str:
    return json.dumps(dict(record), sort_keys=True)


def channel_version(channels: Mapping[str, Any], channel: str) -> Optional[str]:
    """The number a channel stood at when last read, or None."""
    entry = channels.get(channel) if channel in CHANNELS else None
    version = entry.get("version") if isinstance(entry, Mapping) else None
    return version if parse_version(version) else None


@dataclass(frozen=True)
class Target:
    """What a slot's Claude Code should be.

    ``version`` is what the agent is told: a channel name, an exact version, or
    "" to leave it alone. ``channel`` is that channel, or "" for an exact pin.
    ``held`` is the operator's exact pin on a shared machine: nobody else moves
    it, so the page offers nothing.
    """

    version: str
    channel: str
    held: bool


def slot_target(slot: Mapping[str, Any], node: Mapping[str, Any]) -> Target:
    pin = str(node.get("pinned_version") or "").strip()
    if slot.get("kind") == slotstates.OWNER_SLOT:
        # Their own node: the owner's pin is their choice, never a hold.
        return Target(pin, pin if pin in CHANNELS else "", False)
    if pin and pin not in CHANNELS:
        return Target(pin, "", True)
    chosen = slot.get("claude_channel")
    if chosen in CHANNELS:
        return Target(str(chosen), str(chosen), False)
    if pin in CHANNELS:
        return Target(pin, pin, False)
    return Target("", "", False)


#: What the page's Claude Code row can say, one of these.
STATUSES = ("held", "updating", "updated", "failed", "available", "current")


def status(installed: Any, target: Target, channels: Mapping[str, Any],
           update: Optional[Mapping[str, Any]], restart_waiting: bool) -> Optional[dict[str, Any]]:
    """What the Claude Code row says, or None when there is nothing to say yet.

    Strongest first: the hold, an update in flight, the last one's failure, an
    update finished while Remote Control still runs the old one, a newer
    release, and otherwise up to date.
    """
    if parse_version(installed) is None:
        return None
    latest = channel_version(channels, "latest")
    base = {"installed": str(installed), "channel": target.channel, "latest": latest,
            "pinned": "" if target.channel else target.version}
    if target.held:
        return {**base, "status": "held"}
    state = (update or {}).get("state")
    if state == "pending":
        return {**base, "status": "updating", "to": (update or {}).get("to_version") or latest}
    if state == "failed":
        return {**base, "status": "failed", "detail": (update or {}).get("detail") or ""}
    if state == "done" and restart_waiting:
        return {**base, "status": "updated",
                "to": (update or {}).get("to_version") or str(installed)}
    if is_newer(latest, installed):
        return {**base, "status": "available"}
    return {**base, "status": "current"}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
