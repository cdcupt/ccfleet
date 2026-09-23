"""Validation of heartbeat payloads posted by node agents.

Only known fields with expected types are kept. Everything else is dropped so a
compromised or buggy agent cannot smuggle large or odd data into the store.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Optional

from .config import CONTACT_EMAIL_RE
from .slots import MACHINE_MODE
from .store import UNIX_USER_RE

MAX_STR = 200
# Larger than any real measurement, small enough to stay a float.
MAX_NUMBER = 10 ** 15
# One field genuinely needs more room. A real sign-in URL carries the client id,
# both redirect URIs, the full scope list, a PKCE challenge and the state
# parameter: measured at 496 characters against a live node. Capped at MAX_STR it
# arrives truncated, which is worse than absent — it still looks like a URL.
MAX_URL = 1024
MAX_SECRET = 512
# Usage is counted from local transcripts on the node. What arrives is token
# counts, per-day totals and model names; the transcripts themselves hold
# conversation content and never leave the machine.
MAX_USAGE_DAYS = 31
MAX_USAGE_HOURS = 31 * 24
MAX_USAGE_MODELS = 8
USAGE_COUNTERS = ("total_tokens", "input_tokens", "output_tokens",
                  "cache_read_input_tokens", "cache_creation_input_tokens", "sessions")
# A shared machine reports each of its slots. Bounded well past any capacity
# an operator would declare, so one machine cannot post an unbounded list.
MAX_SLOT_REPORTS = 64
# The longest address SMTP allows. A longer one is not an address, and cutting
# it short would show the holder somebody else's.
MAX_EMAIL = 254


class HeartbeatError(ValueError):
    """Raised when the payload is not a usable heartbeat."""


def _str(value: Any, limit: int = MAX_STR) -> Optional[str]:
    if isinstance(value, str):
        return value[:limit]
    return None


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # Python integers are arbitrary precision, so an authenticated node can post
    # a 400-digit one. Anything that later does float arithmetic on it — the
    # dashboard's charts, for instance — raises OverflowError and takes the page
    # down, and math.isnan below would raise on it first anyway. Every field
    # here is a measurement, and none of them is legitimately this large.
    if isinstance(value, int) and abs(value) > MAX_NUMBER:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _bool_or_none(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) or value is None else None


def _section(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    section = payload.get(key)
    return section if isinstance(section, Mapping) else {}


def _quota(section: Mapping[str, Any]) -> dict[str, Any]:
    """Subscription windows, as /usage on the node reported them.

    A percentage and a human reset string per window, nothing else. These come
    from Claude Code reporting on itself, not from any usage endpoint.
    """
    out: dict[str, Any] = {}
    for name in ("session", "week"):
        window = section.get(name)
        if not isinstance(window, Mapping):
            continue
        used = _num(window.get("used_pct"))
        if used is None or not 0 <= used <= 100:
            continue
        out[name] = {"used_pct": used, "resets": _str(window.get("resets"), 40)}
    checked = _num(section.get("checked_at"))
    if checked is not None:
        out["checked_at"] = checked
    return out


def _usage(section: Mapping[str, Any]) -> dict[str, Any]:
    """Counts only, each bounded. A node cannot post an unbounded series here."""
    out: dict[str, Any] = {k: _num(section.get(k)) for k in USAGE_COUNTERS}
    out["window_days"] = _num(section.get("window_days"))
    models = section.get("models")
    out["models"] = ([_str(m, 40) for m in models
                      if isinstance(m, str)][:MAX_USAGE_MODELS]
                     if isinstance(models, list) else [])
    days = section.get("by_day")
    series = []
    if isinstance(days, list):
        for entry in days[-MAX_USAGE_DAYS:]:
            if not isinstance(entry, Mapping):
                continue
            day, tokens = _str(entry.get("day"), 10), _num(entry.get("tokens"))
            if day and tokens is not None:
                series.append({"day": day, "tokens": tokens})
    out["by_day"] = series
    # The hourly series: when its first hour began, then one count per hour.
    # Kept whole or not at all — a series cut short would put every bar in the
    # wrong hour — and a count that is not a sane number counts as nothing.
    out["window_hours"] = _num(section.get("window_hours"))
    hourly = section.get("by_hour")
    if isinstance(hourly, Mapping):
        start, counts = _num(hourly.get("start")), hourly.get("tokens")
        if (start is not None and isinstance(counts, list)
                and 0 < len(counts) <= MAX_USAGE_HOURS):
            out["by_hour"] = {"start": start,
                              "tokens": [max(0, _num(c) or 0) for c in counts]}
    return out


def _email(value: Any) -> str:
    """An address, whole, or nothing. Never a truncated one."""
    if (isinstance(value, str) and len(value) <= MAX_EMAIL and value.isprintable()
            and CONTACT_EMAIL_RE.match(value)):
        return value
    return ""


def _slot_credentials(section: Mapping[str, Any]) -> dict[str, Any]:
    """What a slot's login looks like from outside: the same narrow facts a
    node reports about its own, plus two its holder's page shows them.

    A slot is signed in to one Claude account, its holder's own. The page says
    which — the address, whole, or nothing — and how long its sign-in lasts
    before Anthropic asks for a fresh one. An owner node never sends either
    (see validate_heartbeat), and the console shows neither.
    """
    return {
        "present": _bool_or_none(section.get("present")),
        "logged_in": _bool_or_none(section.get("logged_in")),
        "auth_method": _str(section.get("auth_method"), 40),
        "subscription_type": _str(section.get("subscription_type"), 40),
        "expires_at": _num(section.get("expires_at")),
        "mtime": _num(section.get("mtime")),
        "email": _email(section.get("email")),
        "refresh_expires_at": _num(section.get("refresh_expires_at")),
    }


#: What a slot says about restarting Remote Control onto a new version.
RESTART_STATES = ("waiting", "done")


def _upgrade(section: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """What an agent did about its pin, or None when it reported nothing.

    None rather than a skeleton of Nones: a truthy empty record would make
    "has this ever reconciled?" unanswerable.
    """
    if not any(section.get(k) is not None for k in ("from", "to", "ok", "error", "ts")):
        return None
    return {"from": _str(section.get("from")), "to": _str(section.get("to")),
            "ok": _bool_or_none(section.get("ok")), "error": _str(section.get("error")),
            "ts": _num(section.get("ts"))}


def _slots(value: Any) -> list[dict[str, Any]]:
    """One entry per slot on a shared machine, each about one Linux user.

    The user name is the key the server matches on, so an entry without a
    valid one is dropped rather than guessed at, and a name reported twice
    keeps its first entry: a second one could only be a buggy or hostile
    agent trying to say two things about the same person's slot.
    """
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in value[:MAX_SLOT_REPORTS]:
        if not isinstance(entry, Mapping):
            continue
        user = entry.get("unix_user")
        if not isinstance(user, str) or not UNIX_USER_RE.match(user) or user in seen:
            continue
        seen.add(user)
        out.append({
            "unix_user": user,
            # Whether the Linux user exists. The machine answers this itself,
            # as root, from the account database — not from anything the slot's
            # holder can write.
            "present": _bool_or_none(entry.get("present")),
            # Which claim the machine finished (or failed) setting up, named by
            # the claim's own timestamp so news about an old claim cannot
            # complete a new one.
            "provisioned_for": _num(entry.get("provisioned_for")),
            "provision_failed_for": _num(entry.get("provision_failed_for")),
            "provision_error": _str(entry.get("provision_error")),
            "wipe_error": _str(entry.get("wipe_error")),
            "claude": {"version": _str(_section(entry, "claude").get("version"), 40)},
            "credentials": _slot_credentials(_section(entry, "credentials")),
            "remote_control": {"state": _str(_section(entry, "remote_control").get("state"),
                                             40)},
            "quota": _quota(_section(entry, "quota")),
            "usage": _usage(_section(entry, "usage")),
        })
        progress = _login_progress(_section(entry, "login"))
        if progress:
            out[-1]["login"] = progress
        said = _section(entry, "upgrade")
        upgrade = _upgrade(said)
        restart = said.get("restart") if said.get("restart") in RESTART_STATES else None
        # A restart can be owed after the record that caused it is gone: the
        # record is dropped once the pin is satisfied, the restart once done.
        if upgrade is not None or restart is not None:
            out[-1]["upgrade"] = {**(upgrade or {}), "restart": restart}
    return out


def _login_progress(login: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Sign-in progress, for a node or for one of a machine's slots.

    The URL is shown to a person and the detail may quote the CLI, so both are
    capped like every other supplied string. The secret is a minted device
    token on its way to whoever asked for it: bounded here, redacted before
    the heartbeat is kept, and never logged.
    """
    state = _str(login.get("state"))
    if not state:
        return None
    return {"state": state,
            "url": _str(login.get("url"), MAX_URL),
            "detail": _str(login.get("detail")),
            "requested_at": _num(login.get("requested_at")),
            "secret": _str(login.get("secret"), MAX_SECRET)}


def validate_heartbeat(payload: Any, node_id: str) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise HeartbeatError("heartbeat body must be a JSON object")
    if payload.get("node_id") != node_id:
        raise HeartbeatError("node_id in body does not match the authenticated node")
    claude = _section(payload, "claude")
    creds = _section(payload, "credentials")
    disk = _section(payload, "disk")
    mem = _section(payload, "mem")
    egress = _section(payload, "egress")
    rc = _section(payload, "remote_control")
    load = _section(payload, "load")
    reconcile = _section(payload, "reconcile")
    upgrade = _section(reconcile, "upgrade")
    login = _section(reconcile, "login")
    usage = _section(payload, "usage")
    quota = _section(payload, "quota")
    # Sign-in progress. The URL is shown to an operator and the detail may quote
    # the CLI, so both are length-capped like every other node-supplied string.
    login_state = _str(login.get("state"))
    result: dict[str, Any] = {
        "node_id": node_id,
        "agent_ts": _num(payload.get("ts")),
        "agent_version": _str(payload.get("agent_version")),
        "hostname": _str(payload.get("hostname")),
        "uptime_s": _num(payload.get("uptime_s")),
        "claude": {"version": _str(claude.get("version")), "path": _str(claude.get("path"))},
        "credentials": {
            "present": _bool_or_none(creds.get("present")),
            "store": _str(creds.get("store")),
            "mtime": _num(creds.get("mtime")),
            "expires_at": _num(creds.get("expires_at")),
            "subscription_type": _str(creds.get("subscription_type")),
            "profile_fetched_at": _num(creds.get("profile_fetched_at")),
            "plan": _str(creds.get("plan")),
            # From `claude auth status`: the CLI's own answer, not an inference
            # from a file existing. Deliberately no email, org name or org id.
            "logged_in": _bool_or_none(creds.get("logged_in")),
            "auth_method": _str(creds.get("auth_method")),
            "api_provider": _str(creds.get("api_provider")),
        },
        "disk": {"used_pct": _num(disk.get("used_pct")), "free_gb": _num(disk.get("free_gb"))},
        "mem": {"used_pct": _num(mem.get("used_pct"))},
        "load": {"1": _num(load.get("1")), "5": _num(load.get("5")), "15": _num(load.get("15"))},
        "egress": {"ip": _str(egress.get("ip")), "source": _str(egress.get("source"))},
        "remote_control": {"state": _str(rc.get("state"))},
        "tmux_sessions": _num(payload.get("tmux_sessions")),
        "usage": _usage(usage),
        "quota": _quota(quota),
    }
    # Only a shared machine carries these, and only when it says it is one, so
    # every ordinary node's stored heartbeat keeps exactly the shape it had.
    if payload.get("mode") == MACHINE_MODE:
        result["mode"] = MACHINE_MODE
        result["slots"] = _slots(payload.get("slots"))
    # Whether the OS asked for a reboot. A strict bool or nothing: "yes", 1 or a
    # string from an agent that got it wrong is not a reboot anybody asked for.
    if isinstance(payload.get("reboot_required"), bool):
        result["reboot_required"] = payload["reboot_required"]
    if login_state:
        result["reconcile"] = {"login": _login_progress(login)}
    upgraded = _upgrade(upgrade)
    if upgraded is not None:
        # What the agent did about the last desired state it was handed. Reported
        # one beat late by construction: the agent acts after posting.
        result.setdefault("reconcile", {})["upgrade"] = upgraded
    return result
